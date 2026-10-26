"""
ntfy_installer.py — Install, detect, upgrade, and uninstall the ntfy server
alongside Aerodrome.

Design principles:
  - Idempotent. Running install() when already installed is a no-op that
    returns the current state. Same for uninstall() when not installed.
  - Explicit. install_status() returns a dict describing every file and
    service we'd touch. No hidden state.
  - Verifiable. Downloads are checked against SHA256 hashes published
    alongside the release by the ntfy project.
  - Respectful of externally-managed installs. If the user installed
    ntfy themselves (via apt, snap, a Go build, etc.) and it's running
    at a path we don't recognize, we report that and offer no
    install/uninstall actions — only "use existing".
  - No sudo inside this module. All privileged operations (moving files
    to /usr/local/bin, writing systemd units, systemctl commands) are
    executed through subprocess with sudo; the install.sh sudoers rule
    must permit them. Callers are server.py endpoints running as the
    aerodrome user.

Paths we manage:
  /usr/local/bin/ntfy                    — the binary
  /etc/ntfy/server.yml                   — the minimal config
  /etc/systemd/system/ntfy.service       — the systemd unit
  /var/lib/ntfy/                         — the install stamp and ntfy's message
                                           cache (SERVER_CACHE_FILE, which
                                           server.yml names)
  /var/cache/ntfy/                       — the download staging dir

Public API:
  install_status() → dict
      Describes what's present on disk + whether the service is running.
  install(port: int = 2586, bind: str = "0.0.0.0", topic: str = None) → dict
      Download, verify, install, configure, start. Idempotent.
  update_config(base_url, upstream_relay, port, bind, drop_custom_keys) → dict
      Re-render server.yml from Aerodrome's managed values and restart.
      Refuses (writes nothing) while the file has settings Aerodrome
      doesn't manage, unless drop_custom_keys names every one of them.
  current_config() → dict | None
      classify_server_yml() of the live server.yml (names only).
  upgrade() → dict
      Pull the latest release and swap the binary. Restarts the service.
  uninstall() → dict
      Stop, disable, remove files. Does not remove the binary if it's
      a system-managed package (we never touch apt-installed ntfy).
  latest_version() → str
      Query the GitHub API for the latest tag. Cached per process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import requests

log = logging.getLogger("aerodrome.ntfy_installer")

# ---------------------------------------------------------------
# Paths we manage. All absolute; no variation.
# ---------------------------------------------------------------
BINARY_PATH = Path("/usr/local/bin/ntfy")
CONFIG_DIR = Path("/etc/ntfy")
CONFIG_FILE = CONFIG_DIR / "server.yml"
SYSTEMD_UNIT = Path("/etc/systemd/system/ntfy.service")
CACHE_DIR = Path("/var/cache/ntfy")
# The message cache server.yml names while ntfy runs from the root unit above.
# The renderer takes it from this constant only, so this line is the source of
# truth: the stale-data note and "Also delete cached messages" derive their
# paths from it (ntfy_purge_argvs), uninstall.sh's purge list must keep this
# path, and the release gate (check_installers.py ntfy-cache) reads it from
# this assignment. Every release in git history wrote this same path.
SERVER_CACHE_FILE = "/var/lib/ntfy/cache.db"
# Stamp file — presence indicates Aerodrome-managed install vs external.
# Contents are the ntfy version string we installed.
STAMP_FILE = Path("/var/lib/ntfy/aerodrome-installed")
STAMP_DIR = STAMP_FILE.parent
# Staging path for the downloaded binary before it's installed to BINARY_PATH.
# Fixed path (not a temp dir) so the sudoers rule can whitelist exactly this
# source path — no wildcards, no broader /tmp/* rule needed. The owning dir
# is created at install time and cleaned up after.
STAGING_DIR = CACHE_DIR / "staging"
STAGING_BINARY = STAGING_DIR / "ntfy"

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

# ---------------------------------------------------------------
# Release fetch
# ---------------------------------------------------------------
GITHUB_RELEASES_LATEST = "https://api.github.com/repos/binwiederhier/ntfy/releases/latest"
DOWNLOAD_TIMEOUT_SECONDS = 60
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
    """Small helper: present-ness + mtime for reporting."""
    try:
        st = path.stat()
        return {"present": True, "path": str(path), "size": st.st_size, "mtime": int(st.st_mtime)}
    except FileNotFoundError:
        return {"present": False, "path": str(path)}


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
    """
    bin_info = _file_info(BINARY_PATH)
    cfg_info = _file_info(CONFIG_FILE)
    unit_info = _file_info(SYSTEMD_UNIT)
    stamp_info = _file_info(STAMP_FILE)
    service_info = _systemd_service_status()
    version = _binary_version()
    arch = _detect_arch()

    # Decide state:
    #   - aerodrome_managed: stamp exists AND binary exists
    #   - external: binary exists (in ANY path) but no stamp → user-managed
    #   - not_installed: nothing present, nothing running
    #   - partial: some of our files exist but not all — needs repair
    has_stamp = stamp_info["present"]
    has_bin = bin_info["present"]
    has_cfg = cfg_info["present"]
    has_unit = unit_info["present"]

    if has_stamp and has_bin and has_cfg and has_unit:
        state = "aerodrome_managed"
    elif has_stamp and not (has_bin and has_cfg and has_unit):
        state = "partial"
    elif has_bin and not has_stamp:
        state = "external"
    elif not any([has_bin, has_cfg, has_unit, has_stamp]):
        state = "not_installed"
    else:
        state = "partial"

    # Stamp file may contain the version we wrote at install time
    stamp_version = None
    if has_stamp:
        try:
            stamp_version = STAMP_FILE.read_text().strip()
        except Exception:
            stamp_version = None
    stamp_info["version_installed"] = stamp_version

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
    }


# ---------------------------------------------------------------
# Install
# ---------------------------------------------------------------

def _generate_topic() -> str:
    """Random hard-to-guess topic name. 8 urlsafe characters (~48 bits),
    prefixed with 'aerodrome-' so it's identifiable in the ntfy logs."""
    suffix = secrets.token_urlsafe(6)  # ~8 chars after base64url encoding
    return f"aerodrome-{suffix}"


def _sudo_run(cmd: list, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a sudo command, capturing output. Non-zero exit raises
    RuntimeError with a helpful message; we don't let the caller see
    the raw CompletedProcess so error formatting is consistent."""
    full_cmd = ["sudo", "-n"] + cmd  # -n = fail instead of prompt
    try:
        r = subprocess.run(full_cmd, input=input_text, capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}")
    if r.returncode != 0:
        stderr = (r.stderr or "").strip()
        stdout = (r.stdout or "").strip()
        msg = stderr or stdout or f"rc={r.returncode}"
        # A common failure mode: sudoers rule doesn't permit this command.
        # Make the error actionable.
        if "a password is required" in stderr or "sudo:" in stderr:
            recover = Path(__file__).resolve().parent / "scripts" / "recover-sudoers.sh"
            raise RuntimeError(
                f"sudo refused '{' '.join(cmd)}'. "
                f"Run `sudo bash {shlex.quote(str(recover))}` on the server "
                f"(it refreshes Aerodrome's system permissions only), or the "
                f"command shown on Aerodrome's Updates page. Raw error: {msg}"
            )
        raise RuntimeError(f"Command failed: {' '.join(cmd)}: {msg}")
    return r


def _download_and_verify(version: str, arch: str, dest: Path) -> Path:
    """Download the ntfy archive to dest/; verify its SHA256 against the
    digest GitHub returns in the release API; return the path to the
    extracted binary (still inside dest/).

    v2.40.4 rewrite. The old implementation expected a separate
    `ntfy_{version}_checksums.txt` file as a release artifact, but ntfy
    doesn't publish one — the releases page only contains the binary
    archives themselves, and SHA256 digests are exposed per-asset via
    GitHub's own release metadata (added June 2025). So we now fetch
    the release info from the GitHub API, find the matching asset's
    `digest` field, download the archive, and verify locally. One API
    call, one binary download, same security guarantee.
    """
    tag = f"v{version}"
    base = f"ntfy_{version}_{arch}"
    archive_name = f"{base}.tar.gz"
    archive_url = f"https://github.com/binwiederhier/ntfy/releases/download/{tag}/{archive_name}"
    archive_path = dest / archive_name

    log.info("Fetching release metadata for ntfy %s", version)
    # GitHub release metadata — each asset includes a `digest` field like
    # "sha256:31798741da8ee81a0adb667f77920c1f8604af24b119e2de14352ac250ad89ce"
    api_url = f"https://api.github.com/repos/binwiederhier/ntfy/releases/tags/{tag}"
    try:
        r = requests.get(api_url, timeout=DOWNLOAD_TIMEOUT_SECONDS,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        release_info = r.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to fetch release metadata: {api_url}: {e}")

    expected_hash: Optional[str] = None
    for asset in release_info.get("assets", []):
        if asset.get("name") == archive_name:
            digest = asset.get("digest") or ""
            # GitHub returns digests as "sha256:<hex>". Strip the prefix.
            if digest.startswith("sha256:"):
                expected_hash = digest[7:].lower()
            break
    if not expected_hash:
        # The digest field was added to GitHub's API in June 2025. For
        # very old releases predating that, fall back to a clear error
        # rather than silently skipping verification.
        raise RuntimeError(
            f"Couldn't find SHA256 digest for {archive_name} in release metadata. "
            f"The release may predate GitHub's asset-digest feature, or the "
            f"asset name may have changed upstream."
        )

    log.info("Downloading ntfy %s for %s", version, arch)
    try:
        r = requests.get(archive_url, timeout=DOWNLOAD_TIMEOUT_SECONDS, stream=True)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Download failed: {archive_url}: {e}")
    with archive_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)

    # SHA256 verification against GitHub's published digest
    h = hashlib.sha256()
    with archive_path.open("rb") as f:
        while True:
            buf = f.read(64 * 1024)
            if not buf: break
            h.update(buf)
    actual_hash = h.hexdigest().lower()
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Checksum mismatch for {archive_name}: "
            f"expected {expected_hash}, got {actual_hash}"
        )
    log.info("SHA256 verified for %s", archive_name)

    # Extract with tar — native tool, available everywhere
    r = subprocess.run(["tar", "-xzf", str(archive_path), "-C", str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tar extraction failed: {r.stderr.strip()}")

    # The archive extracts to dest/ntfy_X.Y.Z_arch/ntfy
    extracted_bin = dest / f"ntfy_{version}_{arch}" / "ntfy"
    if not extracted_bin.is_file():
        # Occasionally releases have slightly different internal layout —
        # fall back to a recursive find.
        found = list(dest.rglob("ntfy"))
        # Filter for executable files only
        found_exec = [p for p in found if p.is_file()]
        if not found_exec:
            raise RuntimeError(
                f"Could not find 'ntfy' binary inside extracted archive at {dest}"
            )
        extracted_bin = found_exec[0]

    return extracted_bin


def _systemd_unit_text() -> str:
    return """[Unit]
Description=ntfy server (managed by Aerodrome)
After=network.target

[Service]
ExecStart=/usr/local/bin/ntfy serve --config /etc/ntfy/server.yml
Restart=on-failure
RestartSec=5
# Run as root by default to allow binding to low ports if reconfigured.
# The config file itself restricts what ntfy actually does.

[Install]
WantedBy=multi-user.target
"""


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


def _server_yml_text(port: int, bind: str,
                     base_url: Optional[str] = None,
                     upstream_relay: bool = True) -> str:
    """The server.yml Aerodrome writes, via the shared render_server_yml()
    (validated, JSON-quoted values; fixed comment text).

    cache-file stays SERVER_CACHE_FILE while ntfy runs from the root unit.

    base_url — external URL phones use to reach this server. None means a
      fresh install: auto-detect the LAN IP, falling back to localhost when
      detection fails (the Notifications tab shows detected_lan_ip_error and
      offers a one-click fix). Every other caller passes a validated value.

    upstream_relay — adds upstream-base-url: https://ntfy.sh so iOS clients
      get real-time push (ntfy.sh sees topic and message IDs, never bodies).

    Raises ValueError for any value the renderer refuses.
    """
    if base_url is None:
        lan_ip, _err = _detect_lan_ip()
        base_url = f"http://{lan_ip or 'localhost'}:{port}"
    return render_server_yml(port, bind, base_url, bool(upstream_relay),
                             cache_file=SERVER_CACHE_FILE)


def _validate_install_args(port, bind, base_url) -> None:
    """Raise ValueError unless port/bind/base_url are values the renderer
    will accept. Runs before any sudo call."""
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
            falls back to localhost with a warning. Otherwise it must pass
            validate_base_url().
    upstream_relay — (v2.40.5) if True (default), adds upstream-base-url:
            https://ntfy.sh so iOS clients get real-time APNs push.

    Returns a dict: {'ok', 'message', 'status' (install_status dict),
    'topic' (the one to subscribe to), 'version' (installed)}.
    A value the config renderer would refuse returns {'ok': False,
    'message'} before anything is downloaded or any sudo call is made.
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

    version = latest_version()
    if not version:
        return {
            "ok": False,
            "message": "Could not determine the latest ntfy version from GitHub. "
                       "Check internet connectivity and try again.",
            "status": status,
        }

    topic = topic or _generate_topic()

    # --- Create the cache dir (owned by the aerodrome user so we can write
    # the staged binary into it without another sudo call). The sudoers rule
    # pins the owner to the current user: the account this process runs as,
    # not an inherited $LOGNAME/$USER (which getpass.getuser() reads first). ---
    import getpass
    import pwd
    try:
        aero_user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:    # no passwd entry for our uid (directory offline)
        aero_user = getpass.getuser()
    try:
        _sudo_run(["install", "-d", "-m", "0755",
                   "-o", aero_user, "-g", aero_user, str(CACHE_DIR)])
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to create cache dir: {e}",
                "status": install_status()}
    except OSError as e:
        return {"ok": False,
                "message": f"Failed to create staging dir: {e}",
                "status": install_status()}

    # --- Download + verify in a python tempdir (as aerodrome user, no sudo),
    # then copy the extracted binary into the whitelisted staging path. ---
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            extracted_bin = _download_and_verify(version, status["arch"], tmp)
        except Exception as e:
            log.exception("Download/verify failed")
            return {"ok": False,
                    "message": f"Download or verification failed: {e}",
                    "status": install_status()}

        # Move extracted binary to the fixed staging path. Done as the
        # aerodrome user (no sudo) since the staging dir is user-owned.
        try:
            shutil.copy2(extracted_bin, STAGING_BINARY)
            STAGING_BINARY.chmod(0o755)
        except OSError as e:
            return {"ok": False,
                    "message": f"Failed to stage binary: {e}",
                    "status": install_status()}

    # --- Install binary from the staging path (sudoers whitelists exactly
    # this source → dest pair). ---
    try:
        _sudo_run(["install", "-m", "0755", str(STAGING_BINARY), str(BINARY_PATH)])
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to install binary: {e}",
                "status": install_status()}
    # Clean up staging (best-effort)
    try:
        STAGING_BINARY.unlink(missing_ok=True)
    except OSError:
        pass

    # --- Create other directories ---
    for d in (CONFIG_DIR, STAMP_DIR):
        try:
            _sudo_run(["install", "-d", "-m", "0755", str(d)])
        except RuntimeError as e:
            return {"ok": False, "message": f"Failed to create {d}: {e}",
                    "status": install_status()}

    # --- Write config via sudo tee ---
    try:
        cfg_text = _server_yml_text(port, bind, base_url=base_url,
                                    upstream_relay=upstream_relay)
        _sudo_run(["tee", str(CONFIG_FILE)], input_text=cfg_text)
    except (RuntimeError, ValueError) as e:
        return {"ok": False, "message": f"Failed to write config: {e}",
                "status": install_status()}

    # --- Write systemd unit ---
    # (From here on server.yml is written: failures say so with written=True.)
    try:
        _sudo_run(["tee", str(SYSTEMD_UNIT)], input_text=_systemd_unit_text())
    except RuntimeError as e:
        return {"ok": False, "written": True,
                "message": f"Failed to write systemd unit: {e}",
                "status": install_status()}

    # --- Stamp file (marks this install as aerodrome-managed) ---
    try:
        _sudo_run(["tee", str(STAMP_FILE)], input_text=version + "\n")
    except RuntimeError as e:
        return {"ok": False, "written": True, "message": f"Failed to write stamp: {e}",
                "status": install_status()}

    # --- Enable and start service ---
    try:
        _sudo_run(["systemctl", "daemon-reload"])
        _sudo_run(["systemctl", "enable", "ntfy"])
        _sudo_run(["systemctl", "start", "ntfy"])
    except RuntimeError as e:
        return {"ok": False, "written": True,
                "message": f"Failed to enable/start service: {e}",
                "status": install_status()}

    new_status = install_status()
    return {
        "ok": True,
        "message": f"ntfy {version} installed and started. "
                   f"Subscribe from the ntfy mobile app.",
        "status": new_status,
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

    Returns {'ok', 'message', 'base_url', 'upstream_relay', 'port', 'bind',
    'dropped_keys'} on success. When the file was written but ntfy didn't
    restart: {'ok': False, 'written': True, 'dropped_keys', 'message'}.
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
        return {
            "ok": False, "code": "custom_keys", "custom_keys": cur["custom"],
            "message": "Your notification server's config file has settings Aerodrome "
                       f"doesn't manage: {', '.join(cur['custom'])}. "
                       "Saving would remove them.",
        }

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
        cfg_text = render_server_yml(port, bind, base_url, upstream_relay,
                                     cache_file=SERVER_CACHE_FILE)
    except ValueError as e:
        return {"ok": False, "code": "invalid", "message": str(e)}

    try:
        _sudo_run(["tee", str(CONFIG_FILE)], input_text=cfg_text)
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to write config: {e}"}

    try:
        _sudo_run(["systemctl", "restart", "ntfy"])
    except RuntimeError as e:
        return {"ok": False, "written": True, "dropped_keys": cur["custom"],
                "message": f"Config written but failed to restart: {e}"}

    return {
        "ok": True,
        "message": "ntfy config updated and service restarted.",
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
    Pulls latest version, verifies, swaps the binary, restarts the service.
    """
    status = install_status()
    if status["state"] != "aerodrome_managed":
        return {"ok": False,
                "message": f"Cannot upgrade from state '{status['state']}'. "
                           "Upgrade is only available for Aerodrome-managed installs.",
                "status": status}

    latest = latest_version()
    if not latest:
        return {"ok": False,
                "message": "Could not determine latest ntfy version.",
                "status": status}
    current = status.get("version") or status["stamp"].get("version_installed")
    if current == latest:
        return {"ok": True,
                "message": f"Already on the latest ntfy ({latest}).",
                "status": status, "version": latest}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            extracted_bin = _download_and_verify(latest, status["arch"], tmp)
        except Exception as e:
            return {"ok": False, "message": f"Download failed: {e}",
                    "status": install_status()}
        # Stage into the whitelisted path
        try:
            STAGING_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extracted_bin, STAGING_BINARY)
            STAGING_BINARY.chmod(0o755)
        except OSError as e:
            return {"ok": False,
                    "message": f"Failed to stage upgraded binary: {e}",
                    "status": install_status()}

    try:
        _sudo_run(["install", "-m", "0755", str(STAGING_BINARY), str(BINARY_PATH)])
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to replace binary: {e}",
                "status": install_status()}
    # Clean up staging (best-effort)
    try:
        STAGING_BINARY.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        _sudo_run(["tee", str(STAMP_FILE)], input_text=latest + "\n")
        _sudo_run(["systemctl", "restart", "ntfy"])
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to restart service: {e}",
                "status": install_status()}

    return {"ok": True,
            "message": f"Upgraded ntfy {current or '?'} → {latest}.",
            "status": install_status(), "version": latest}


def uninstall(purge_data: bool = False) -> Dict[str, Any]:
    """Remove Aerodrome-managed ntfy install. Safe to run from any state —
    missing files are ignored. Refuses to touch external installs.

    purge_data (v2.41.0) — if True, also delete ntfy's cached messages
    (ntfy_purge_argvs: SERVER_CACHE_FILE and its -wal/-shm). Default False
    so users who reinstall later keep their topic's message history.
    purged_data in the result is True only when that deletion went through.

    The stamp goes last, and only when every earlier step worked: anything
    left behind (a refused purge, a file sudo wouldn't remove) keeps the
    install 'partial', whose panel offers Uninstall again — without the
    stamp a leftover binary would read as 'external' (never touched) and a
    leftover cache as 'not_installed' (no uninstall control)."""
    status = install_status()
    if status["state"] == "external":
        return {"ok": False,
                "message": "Refusing to uninstall: ntfy was installed outside "
                           "Aerodrome. Manage it with your package manager.",
                "status": status}
    if status["state"] == "not_installed":
        # Even when there's no install, honor the purge request if the user
        # somehow has stale data left over from a prior install.
        if purge_data:
            try:
                _purge_ntfy_data()
            except Exception as e:
                return {"ok": False,
                        "message": f"Nothing to uninstall. Deleting cached messages failed: {e}",
                        "status": status, "purged_data": False}
        return {"ok": True, "message": "Nothing to uninstall.", "status": status,
                "purged_data": purge_data}

    # A cache-file set by hand in server.yml (a custom setting) isn't
    # Aerodrome's to delete: the purge removes SERVER_CACHE_FILE only, and the
    # result says the hand-set one was left (its name, never its value). Read
    # before server.yml is removed below.
    hand_cache = purge_data and "cache-file" in ((current_config() or {}).get("custom") or ())

    errors = []
    # Best-effort stop + disable; ignore failures (service may already be gone)
    for cmd in (["systemctl", "stop", "ntfy"],
                ["systemctl", "disable", "ntfy"]):
        try:
            _sudo_run(cmd)
        except RuntimeError as e:
            # Only record as an error if the unit actually exists
            if SYSTEMD_UNIT.exists():
                errors.append(str(e))

    # Remove files (the stamp last, below)
    for path in (SYSTEMD_UNIT, BINARY_PATH, CONFIG_FILE):
        if path.exists():
            try:
                _sudo_run(["rm", "-f", str(path)])
            except RuntimeError as e:
                errors.append(f"rm {path}: {e}")

    # v2.41.0: if user asked to purge data, wipe the message cache
    # (ntfy_purge_argvs). This is destructive — only on explicit opt-in.
    purged = False
    if purge_data:
        try:
            _purge_ntfy_data()
            purged = True
        except Exception as e:
            errors.append(f"purge data: {e}")

    # The stamp only once everything above worked (see the docstring).
    if not errors and STAMP_FILE.exists():
        try:
            _sudo_run(["rm", "-f", str(STAMP_FILE)])
        except RuntimeError as e:
            errors.append(f"rm {STAMP_FILE}: {e}")

    # Remove empty directories — leave them if they contain user data
    for path in (CONFIG_DIR, CACHE_DIR, STAMP_DIR):
        if path.exists():
            try:
                _sudo_run(["rmdir", "--ignore-fail-on-non-empty", str(path)])
            except RuntimeError:
                pass  # not fatal

    # daemon-reload after unit removal
    try:
        _sudo_run(["systemctl", "daemon-reload"])
    except RuntimeError:
        pass  # best-effort

    new_status = install_status()
    if errors:
        return {"ok": False,
                "message": f"Uninstall finished with errors: {'; '.join(errors)}",
                "status": new_status,
                "purged_data": purged}
    msg = "Uninstalled ntfy."
    if purge_data:
        msg += f" Cached message data at {Path(SERVER_CACHE_FILE).parent}/ was also removed."
        if hand_cache:
            msg += " The cache file you set by hand in server.yml (cache-file) was left in place."
    else:
        msg += f" Cached message data at {SERVER_CACHE_FILE} was kept."
    return {"ok": True, "message": msg, "status": new_status,
            "purged_data": purged}


def _sqlite_files(db) -> list:
    """An SQLite database and the -wal/-shm files SQLite keeps beside it."""
    return [Path(f"{db}{side}") for side in ("", "-wal", "-shm")]


def ntfy_purge_argvs() -> list:
    """The commands "Also delete cached messages" runs through sudo -n, one
    per path: `rm -f` on the cache server.yml names (SERVER_CACHE_FILE) and
    SQLite's -wal/-shm beside it — the cache every release wrote and a
    reinstall picks up. sudo matches a rule's whole command line, so
    install.sh's sudoers rules must grant each argv word for word.

    Nothing in /var/cache/ntfy: no Aerodrome release wrote a cache or
    attachments there (the purge looked there by mistake before v3.4.146),
    and ntfy's own package keeps its cache there. uninstall.sh's purge
    removes the same files."""
    return [["rm", "-f", str(p)] for p in _sqlite_files(SERVER_CACHE_FILE)]


def _purge_ntfy_data() -> None:
    """Remove ntfy's cached message data (ntfy_purge_argvs). Tries every
    path that exists — a dangling link too — then raises one RuntimeError
    naming the ones that couldn't be removed (e.g. sudo refused: no rule
    for that path yet)."""
    failed = []
    for argv in ntfy_purge_argvs():
        path = argv[-1]
        if not os.path.lexists(path):
            continue
        try:
            _sudo_run(argv)
        except RuntimeError as e:
            failed.append(f"{path}: {e}")
    if failed:
        raise RuntimeError("couldn't remove " + "; ".join(failed))


def stale_data_present() -> bool:
    """(v2.41.0) Return True if the message cache a reinstall would use
    (SERVER_CACHE_FILE, or its -wal/-shm) exists even though no ntfy install
    is active. Used by the install flow to inform the user their reinstall
    will inherit old cached messages."""
    status = install_status()
    if status["state"] != "not_installed":
        return False
    return any(p.exists() for p in _sqlite_files(SERVER_CACHE_FILE))
