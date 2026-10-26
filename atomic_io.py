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


def stage_bytes(path, data, mode=None):
    """Write `data` to a temp file beside `path` (fsynced) and return its
    name. The temp file takes the existing file's permission bits, else
    `mode`, else 0644. On any error the temp file is removed and the error
    re-raised; `path` itself is never touched here."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".",
                               suffix=".tmp")
    try:
        try:
            perm = os.stat(path).st_mode & 0o7777
        except OSError:
            perm = 0o644 if mode is None else mode
        os.fchmod(fd, perm)
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


def write_atomic(path, data, mode=None):
    tmp = stage_bytes(path, data, mode)
    try:
        commit(tmp, path)
    except BaseException:
        discard(tmp)
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
