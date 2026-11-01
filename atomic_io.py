"""Crash-safe small-file writes (stdlib only).

A write goes to a temp file in the target's directory, is flushed and
fsynced, then renamed over the target, and the directory is fsynced — so a
reader (or a power cut) sees the old file or the new one, never a torn mix.
A disk-full error leaves the old file untouched and no temp file behind.

updater.py deliberately does NOT import this module (it must load from any
half-swapped tree) and keeps a private copy of the JSON writer instead.
"""
import errno
import json
import os
import tempfile
from pathlib import Path

DISK_FULL_ERRNOS = frozenset(e for e in (errno.ENOSPC, getattr(errno, "EDQUOT", None),
                                         errno.EFBIG) if e is not None)


def is_disk_full(exc):
    """True for the OSErrors that mean "out of space" (ENOSPC, EDQUOT, EFBIG)."""
    return isinstance(exc, OSError) and exc.errno in DISK_FULL_ERRNOS


def fsync_dir(d):
    """fsync a directory so a rename inside it survives a power cut. Best effort."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def stage_bytes(path, data, mode=None, keep_mode=True, keep_owner=False):
    """Write `data` to a temp file beside `path` (fsynced) and return its
    name. The temp file takes the existing file's permission bits
    (keep_mode), else `mode`, else 0644. keep_owner: when running as root,
    it also takes the existing file's owner and group, so a root-run command
    never leaves the service account's file owned by root. On any error the
    temp file is removed and the error re-raised; `path` itself is never
    touched here."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".",
                               suffix=".tmp")
    try:
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if keep_mode and st is not None:
            perm = st.st_mode & 0o7777
        else:
            perm = 0o644 if mode is None else mode
        os.fchmod(fd, perm)
        if keep_owner and st is not None and os.geteuid() == 0:
            os.fchown(fd, st.st_uid, st.st_gid)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        discard(tmp)
        raise
    return tmp


def commit(tmp, path):
    """Rename a staged temp file over `path` and fsync the directory."""
    os.replace(tmp, path)
    fsync_dir(Path(path).parent)


def discard(tmp):
    try:
        os.unlink(tmp)
    except OSError:
        pass


def write_atomic(path, data, mode=None, keep_mode=True, keep_owner=False):
    tmp = stage_bytes(path, data, mode, keep_mode=keep_mode, keep_owner=keep_owner)
    try:
        commit(tmp, path)
    except BaseException:
        discard(tmp)
        raise


# --- Private files (v3.4.148) ---
# Backups, upload spools and config.yaml hold the receiver's location, API
# keys and notification credentials, so they are created readable by the
# account Aerodrome runs as only: directories 0700, files 0600.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_dir(d):
    """Create directory `d` if it's missing (parents as needed). A directory
    this call creates is 0700; an existing one is left exactly as it is.
    Returns Path(d)."""
    d = Path(d)
    if d.is_dir():
        return d
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(d, PRIVATE_DIR_MODE)
    except FileExistsError:
        if not d.is_dir():
            raise
        return d
    os.chmod(d, PRIVATE_DIR_MODE)   # exact, whatever the umask
    return d


def open_private(path):
    """Create `path` as a new 0600 file (whatever the umask) and open it for
    writing. It must not exist yet — never follows a symlink or truncates
    another file planted under the name (FileExistsError). Returns a binary
    file."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def write_json_atomic(path, obj, mode=None):
    write_atomic(path, json.dumps(obj, indent=1, sort_keys=True) + "\n", mode)


def read_json(path):
    """A JSON object from `path`, or None when missing, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None
