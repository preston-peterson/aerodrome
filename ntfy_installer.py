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
  /var/cache/ntfy/                       — ntfy's message cache
  /var/lib/ntfy/                         — reserved for ntfy's state

Public API:
  install_status() → dict
      Describes what's present on disk + whether the service is running.
  install(port: int = 2586, bind: str = "0.0.0.0", topic: str = None) → dict
      Download, verify, install, configure, start. Idempotent.
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
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


def _read_server_port() -> Optional[int]:
    """Extract the listen port from /etc/ntfy/server.yml. The relevant
    directive is `listen-http: "bind:port"` or `listen-http: ":port"`.
    Returns the port as int, or None if the config is missing / malformed.
    """
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                m = re.match(r'^listen-http\s*:\s*"?([^"]+)"?\s*$', line)
                if m:
                    addr = m.group(1).strip()
                    # "bind:port" or ":port" → take the last colon segment
                    if ":" in addr:
                        port_str = addr.rsplit(":", 1)[1]
                        try:
                            return int(port_str)
                        except ValueError:
                            return None
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return None


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
            raise RuntimeError(
                f"sudo rejected '{' '.join(cmd)}'. "
                f"The sudoers rule may not permit this command. "
                f"Re-run install.sh to refresh the sudoers entries. Raw error: {msg}"
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
                # 10.254.254.254 is unreachable in any normal config; the
                # kernel still does a route lookup to decide a source IP.
                s.connect(("10.254.254.254", 1))
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


def _server_yml_text(port: int, bind: str, cache_dir: str,
                     base_url: Optional[str] = None,
                     upstream_relay: bool = True) -> str:
    """Minimal ntfy config.

    Important: ntfy's cache-file determines where messages are stored.
    We use /var/lib/ntfy/cache.db. ntfy runs as root so can write there.
    Retention is ntfy's default (12h).

    base_url (v2.40.5) — external URL phones use to reach this server.
      If None, we auto-detect the LAN IP. If detection fails, we fall
      back to localhost with a prominent warning comment. Users can
      override later via the Notifications config UI.

    upstream_relay (v2.40.5) — if True, adds upstream-base-url: https://ntfy.sh
      so iOS clients get real-time push (APNs wake-up via ntfy.sh).
      Without this, iOS only sees messages on manual refresh. Android
      doesn't need it. Privacy note: enabling this means topic IDs
      and message IDs are visible to ntfy.sh — not message content.
    """
    # Resolve base URL. The phone needs a URL it can actually reach;
    # localhost won't work because localhost-on-the-phone isn't the
    # server. Auto-detect LAN IP as the default.
    if base_url is None:
        lan_ip, lan_ip_err = _detect_lan_ip()
        if lan_ip:
            base_url = f"http://{lan_ip}:{port}"
            base_url_comment = (
                "# Auto-detected LAN IP. If your phone can't reach this URL (e.g.\n"
                "# you're on a different network), change it to something reachable\n"
                "# from the phone — a Tailscale IP, a reverse-proxy HTTPS URL, etc."
            )
        else:
            base_url = f"http://localhost:{port}"
            # v3.4.27: include the actual detection failure reason in the
            # config file comment so a sysadmin reading server.yml directly
            # can see WHY auto-detection failed (typically an install-time
            # network race: ENETUNREACH because DHCP hadn't finished).
            err_line = f"# Detection failure: {lan_ip_err}\n" if lan_ip_err else ""
            base_url_comment = (
                "# WARNING: could not auto-detect a LAN IP. Using localhost means\n"
                "# phones CANNOT reach this server. Change base-url to something\n"
                "# reachable from your phone, e.g. http://<server-lan-ip>:{port}\n"
                "# or a Tailscale/reverse-proxy URL.\n"
                "{err}# (Aerodrome's Notifications UI may offer a one-click fix if\n"
                "# re-detection now succeeds — see the wizard at /config.)"
                .format(port=port, err=err_line)
            )
    else:
        base_url_comment = "# base-url set via install options."

    if upstream_relay:
        upstream_block = (
            "# iOS instant-push support. Self-hosted ntfy cannot wake iPhones\n"
            "# directly (APNs is Apple-only). Instead, ntfy.sh is used as a\n"
            "# wake-up relay: when a message arrives here, a poll request is\n"
            "# sent to ntfy.sh with just the message ID — which then triggers\n"
            "# an APNs push on your iPhone. Only the topic + message ID are\n"
            "# shared with ntfy.sh; message bodies stay on this server.\n"
            "# Comment out this line if you don't use iOS or don't want the\n"
            "# topic ID going to ntfy.sh. Android doesn't need it.\n"
            "upstream-base-url: \"https://ntfy.sh\""
        )
    else:
        upstream_block = (
            "# upstream-base-url disabled. iOS clients will only see messages\n"
            "# on manual refresh. Re-enable for iOS real-time push."
        )

    return f"""# Aerodrome-managed ntfy config. Edit at your own risk — future
# upgrades via Aerodrome may overwrite this file.
{base_url_comment}
base-url: "{base_url}"
listen-http: "{bind}:{port}"
cache-file: /var/lib/ntfy/cache.db
cache-duration: 12h
behind-proxy: false
{upstream_block}
"""


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
            falls back to localhost with a warning.
    upstream_relay — (v2.40.5) if True (default), adds upstream-base-url:
            https://ntfy.sh so iOS clients get real-time APNs push.

    Returns a dict: {'ok', 'message', 'status' (install_status dict),
    'topic' (the one to subscribe to), 'version' (installed)}.
    """
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
    # pins the owner to the current user. ---
    import getpass
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
    cfg_text = _server_yml_text(port, bind, str(CACHE_DIR),
                                base_url=base_url,
                                upstream_relay=upstream_relay)
    try:
        _sudo_run(["tee", str(CONFIG_FILE)], input_text=cfg_text)
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to write config: {e}",
                "status": install_status()}

    # --- Write systemd unit ---
    try:
        _sudo_run(["tee", str(SYSTEMD_UNIT)], input_text=_systemd_unit_text())
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to write systemd unit: {e}",
                "status": install_status()}

    # --- Stamp file (marks this install as aerodrome-managed) ---
    try:
        _sudo_run(["tee", str(STAMP_FILE)], input_text=version + "\n")
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to write stamp: {e}",
                "status": install_status()}

    # --- Enable and start service ---
    try:
        _sudo_run(["systemctl", "daemon-reload"])
        _sudo_run(["systemctl", "enable", "ntfy"])
        _sudo_run(["systemctl", "start", "ntfy"])
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to enable/start service: {e}",
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


def update_config(base_url: Optional[str] = None,
                  upstream_relay: Optional[bool] = None) -> Dict[str, Any]:
    """(v2.40.5) Rewrite /etc/ntfy/server.yml with updated base_url
    and/or upstream_relay, preserving other settings. Restarts the
    service so changes take effect.

    Only works for aerodrome_managed installs — externally-managed
    configs are not touched (user is responsible).

    Returns {'ok': bool, 'message': str, 'base_url': str, 'upstream_relay': bool}.
    """
    status = install_status()
    if status["state"] != "aerodrome_managed":
        return {
            "ok": False,
            "message": "ntfy config is not managed by Aerodrome. "
                       "Edit /etc/ntfy/server.yml directly.",
        }

    # Read the existing port + bind from the live config so we don't
    # reset those to defaults. If we can't parse them, bail.
    port = _read_server_port() or 2586
    bind = _read_server_bind() or "0.0.0.0"

    # If the caller passed None for either param, we read the CURRENT
    # value from the existing config and keep it. This way a partial
    # update (e.g. just toggling upstream_relay) doesn't reset base_url.
    if base_url is None:
        base_url = _read_base_url()
    if upstream_relay is None:
        upstream_relay = _read_upstream_relay()

    cfg_text = _server_yml_text(port, bind, str(CACHE_DIR),
                                base_url=base_url,
                                upstream_relay=upstream_relay)
    try:
        _sudo_run(["tee", str(CONFIG_FILE)], input_text=cfg_text)
    except RuntimeError as e:
        return {"ok": False, "message": f"Failed to write config: {e}"}

    try:
        _sudo_run(["systemctl", "restart", "ntfy"])
    except RuntimeError as e:
        return {"ok": False, "message": f"Config written but failed to restart: {e}"}

    return {
        "ok": True,
        "message": "ntfy config updated and service restarted.",
        "base_url": base_url,
        "upstream_relay": upstream_relay,
    }


def _read_server_bind() -> Optional[str]:
    """Extract bind address from listen-http directive. Returns e.g.
    '0.0.0.0' or '127.0.0.1'. Returns None if not set or malformed."""
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                s = line.strip()
                if s.startswith("#") or not s:
                    continue
                m = re.match(r'^listen-http\s*:\s*"?([^"]+)"?\s*$', s)
                if m:
                    addr = m.group(1).strip()
                    if ":" in addr:
                        bind = addr.rsplit(":", 1)[0]
                        return bind or "0.0.0.0"
    except Exception:
        return None
    return None


def _read_base_url() -> Optional[str]:
    """Read the current base-url from server.yml. Returns None if missing."""
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                s = line.strip()
                if s.startswith("#") or not s:
                    continue
                m = re.match(r'^base-url\s*:\s*"?([^"]+?)"?\s*$', s)
                if m:
                    return m.group(1).strip()
    except Exception:
        return None
    return None


def _read_upstream_relay() -> bool:
    """Return True if upstream-base-url is set (uncommented) in server.yml.
    Default True so toggling only one field doesn't inadvertently disable
    upstream relay on a user who had it enabled."""
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                s = line.strip()
                if s.startswith("#") or not s:
                    continue
                if re.match(r'^upstream-base-url\s*:', s):
                    return True
        # Config file exists but no active upstream-base-url line found
        return False
    except Exception:
        return True  # Default on error


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

    purge_data (v2.41.0) — if True, also delete /var/lib/ntfy/cache.db
    and any other runtime data in /var/lib/ntfy/. Default False so
    users who reinstall later keep their topic's message history."""
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
            _purge_ntfy_data()
        return {"ok": True, "message": "Nothing to uninstall.", "status": status}

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

    # Remove files
    for path in (SYSTEMD_UNIT, BINARY_PATH, CONFIG_FILE, STAMP_FILE):
        if path.exists():
            try:
                _sudo_run(["rm", "-f", str(path)])
            except RuntimeError as e:
                errors.append(f"rm {path}: {e}")

    # v2.41.0: if user asked to purge data, wipe cache.db and the cache dir
    # contents. This is destructive — only on explicit opt-in.
    if purge_data:
        try:
            _purge_ntfy_data()
        except Exception as e:
            errors.append(f"purge data: {e}")

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
                "purged_data": purge_data}
    msg = "Uninstalled ntfy."
    if purge_data:
        msg += " Cached message data at /var/lib/ntfy/ was also removed."
    else:
        msg += " Cached message data at /var/lib/ntfy/cache.db was kept."
    return {"ok": True, "message": msg, "status": new_status,
            "purged_data": purge_data}


def _purge_ntfy_data() -> None:
    """Remove ntfy's cached message data. Best-effort; raises on failure."""
    for name in ("cache.db", "cache.db-wal", "cache.db-shm"):
        p = CACHE_DIR / name
        if p.exists():
            _sudo_run(["rm", "-f", str(p)])
    # Also remove any attachments subdirectory ntfy may have created
    attachments = CACHE_DIR / "attachments"
    if attachments.exists():
        _sudo_run(["rm", "-rf", str(attachments)])


def stale_data_present() -> bool:
    """(v2.41.0) Return True if /var/lib/ntfy/cache.db exists even though
    no ntfy install is active. Used by the install flow to inform the
    user their reinstall will inherit old cached messages."""
    status = install_status()
    if status["state"] != "not_installed":
        return False
    return (CACHE_DIR / "cache.db").exists()
