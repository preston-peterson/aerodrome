"""restore_io.py — restoring a full backup can't leave Aerodrome unable to start.

The old restore wrote the backup's config.yaml and database straight over
the live files while the collector and web requests had the database open.
A damaged zip, a full disk, a config from another machine or one that fails
validation left a broken install: a restart loop with the web UI gone, or a
start on an empty history.

A restore now runs in two halves.

  1. In the running server (prepare, called by server.py in the threadpool,
     under the maintenance lock): the backup's settings are checked the way
     startup reads them and by Settings' validator, keeping THIS machine's
     web.host / web.port / data.db_file / logging.dir; its database is
     unpacked (no compression method but stored/deflate: those are the only
     ones whose declared sizes bound what is decompressed) to a
     staging file beside the live one, quick_check'ed, checked for the
     tables Aerodrome needs and for a schema newer than this release; then a
     copy of the live database (SQLite backup API, so commits still in the
     -wal are included) and of config.yaml are kept as `.pre-restore` copies,
     and the marker .backups/pending-restore.json is written. Nothing live
     changes: the live database, config.yaml and the running settings are
     untouched, and any failure removes what it made. (One exception: the
     notification server's settings in the backup are applied at once —
     server._restore_ntfy_part, after every check and the marker; never for
     a database-only restore — since ntfy is a separate service.)
  2. At the next start (main.start, after the startup housekeeping and
     before any database connection exists — apply_pending_restore): the
     old database's -wal/-shm/-journal are removed and the staged files are
     moved into place. If the restored data then fails to load (the database
     won't open, the rollup build or a migration fails, or the process dies
     before it finishes starting twice), the start that notices asks for a
     rollback and starts again; the rollback moves the pre-restore copies
     back. (A web address that can't be listened on is not the restore's
     doing — this machine's web.host and web.port are always kept — so it
     waits, like any start, rather than throwing a good restore away.)
     Either way the result is recorded in .backups/restore-result.json for
     the Settings page.

Each file is kept on ITS OWN filesystem: the staged database and the
database's pre-restore copy beside the database (data.db_file may live on
another disk, or be a symlink to one: the database's REAL path is used, so
the link survives and its target is what is swapped), the staged config and
config.yaml's pre-restore copy beside config.yaml. A swap or a rollback is then a rename, which needs no free
space and can't fail with EXDEV half-way (config.yaml symlinked onto
another filesystem is written through config_io instead). The marker
records every path and the inode of every staged file and copy, so each
step can be repeated after a kill at any point (the next start finishes
it), and a copy that is really gone is reported, never claimed as restored.

A rollback CONSUMES the pre-restore copy: after it, the copy is the live
data again (never a hard link to it, which would change with every write).

A restore staged but not applied (the restart call failed) waits at most
RESTORE_STAGE_TTL for a start, can be cancelled meanwhile, and is then
discarded without touching anything. While one waits, settings writes and
other maintenance are refused (server.py), so the restart can't silently
undo them.

Every config.yaml write here goes through config_io (atomic, 0600).
"""
import errno
import hashlib
import itertools
import os
import re
import sqlite3
import stat
import time
import zipfile
from pathlib import Path

import yaml

import atomic_io
import config_io

MARKER_NAME = config_io.RESTORE_MARKER_NAME          # "pending-restore.json"
RESULT_NAME = "restore-result.json"
MARKER_VERSION = 1
PHASES = frozenset({"staged", "swapped", "rollback"})
SQLITE_MAGIC = b"SQLite format 3\x00"
# What main.load_config() refuses to start without — config_io is the one source.
REQUIRED_SECTIONS = config_io.REQUIRED_SECTIONS
# Tables main.py and the collector read on every start.
REQUIRED_TABLES = ("all_sightings", "military_sightings", "watchlist_sightings")
# Settings that belong to this machine, not to the backup: where the web UI
# listens, where the database lives and where the logs go. Restoring another
# machine's values made the start fail its bind check (a restart loop), put
# the restored history somewhere the service never reads (an empty history),
# or named a log folder this machine can't create (every later start — and
# `main.py stop` — failed before it began).
# DECISION (v3.4.149, extends Decision 4 to logging.dir — the maintainer's
# call): drop ("logging", "dir") from this tuple to restore the backup's.
MACHINE_KEYS = (("web", "host"), ("web", "port"), ("data", "db_file"), ("logging", "dir"))
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 256 * 1024
HEADROOM_BYTES = 256 * 1024 * 1024
RESTORE_STAGE_TTL = 3600          # a staged restore not applied within this is dropped
MAX_SWAPPED_STARTS = 2            # starts that may die before finishing, then roll back
STAGED_SUFFIX = ".restore-staged"
PRE_RESTORE_SUFFIX = ".pre-restore"
_CHUNK = 1024 * 1024
_SIDECARS = ("-wal", "-shm", "-journal")
_TS_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")


class RestoreError(Exception):
    """The restore was refused, or failed before anything live changed."""

    def __init__(self, message, status=400, errors=None, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors = errors or []
        self.code = code or {400: "bad_backup", 409: "newer_schema", 422: "invalid_settings",
                             507: "no_space"}.get(status, "restore_failed")

    def payload(self):
        out = {"ok": False, "code": self.code, "error": self.message, "message": self.message,
               "changed": False}
        if self.errors:
            out["errors"] = self.errors
        return out


class SnapshotMissing(Exception):
    """A pre-restore copy a rollback needs is gone."""


class ApplyError(Exception):
    """A staged file a swap needs is gone (and wasn't moved in already)."""


# ----------------------------------------------------------------- paths

def marker_path(install_dir):
    return Path(install_dir) / ".backups" / MARKER_NAME


def result_path(install_dir):
    return Path(install_dir) / ".backups" / RESULT_NAME


def staged_db_path(db_live):
    db_live = Path(db_live)
    return db_live.with_name(f".{db_live.name}{STAGED_SUFFIX}")


def staged_config_path(config_live):
    config_live = Path(config_live)
    return config_live.with_name(f".{config_live.name}{STAGED_SUFFIX}")


def db_snapshot_path(db_live, ts):
    db_live = Path(db_live)
    return db_live.with_name(f"{db_live.name}.bak.{ts}{PRE_RESTORE_SUFFIX}")


def config_snapshot_path(config_live, ts):
    return Path(config_live).with_name(f"config.yaml.bak.{ts}{PRE_RESTORE_SUFFIX}")


def _sidecars(db):
    return [Path(f"{db}{s}") for s in _SIDECARS]


def _unlink(p):
    try:
        os.unlink(str(p))
        return True
    except FileNotFoundError:
        return False


def _lexists(p):
    return os.path.lexists(str(p))


def _ino(p):
    st = os.stat(str(p))
    return [st.st_dev, st.st_ino]


def _size_if_file(p):
    """A regular file's size, else 0 — one stat, no look first: SQLite
    removes a database's -wal when its last connection closes (the
    collector opens and closes one on every poll), so a -wal seen a moment
    ago may be gone by the time it is measured."""
    try:
        st = os.stat(str(p))
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


def _sha256_file(p):
    h = hashlib.sha256()
    with open(str(p), "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------- marker / result

def _is_ino(v):
    return (isinstance(v, list) and len(v) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in v))


def _opt(v, *types):
    return v is None or (isinstance(v, types) and not isinstance(v, bool))


_PART_STR_KEYS = ("live", "configured", "staged", "snapshot", "staged_sha256",
                  "snapshot_sha256", "set_aside")


def _well_formed(m):
    """The marker has the shape this module reads (every field may be
    missing; one that is there has the right type). Fields are only ever
    added, so an unknown key is fine — a wrong type is a hand edit (or a
    release that broke that rule) and must not crash every start."""
    if not isinstance(m, dict) or not isinstance(m.get("phase"), str):
        return False
    for key in ("db", "config"):
        part = m.get(key)
        if part is None:
            continue
        if not isinstance(part, dict):
            return False
        for k in _PART_STR_KEYS:
            if not _opt(part.get(k), str):
                return False
        for k in ("staged_ino", "snapshot_ino"):
            if part.get(k) is not None and not _is_ino(part.get(k)):
                return False
        if not _opt(part.get("schema_version"), int):
            return False
    return (_opt(m.get("starts"), int) and _opt(m.get("staged_at"), int, float)
            and _opt(m.get("swap_started"), int, float)
            and _opt(m.get("ts"), str) and _opt(m.get("notes"), list)
            and _opt(m.get("result"), dict))


def read_marker(install_dir):
    """The marker, or None when there is none (or it can't be read, or
    doesn't have the shape this release reads — apply_pending_restore then
    sets it aside)."""
    m = atomic_io.read_json(marker_path(install_dir))
    return m if _well_formed(m) else None


def marker_exists(install_dir):
    return _lexists(marker_path(install_dir))


def write_marker(install_dir, marker):
    """Durable (it gates what the next start does); raises OSError."""
    atomic_io.ensure_private_dir(Path(install_dir) / ".backups")
    atomic_io.write_json_atomic(marker_path(install_dir), marker,
                                mode=atomic_io.PRIVATE_FILE_MODE)


def read_result(install_dir):
    return atomic_io.read_json(result_path(install_dir))


def write_result(install_dir, res, log=None):
    """Best effort: an outcome record never stops a start. True when written."""
    try:
        atomic_io.ensure_private_dir(Path(install_dir) / ".backups")
        atomic_io.write_json_atomic(result_path(install_dir), res,
                                    mode=atomic_io.PRIVATE_FILE_MODE)
        return True
    except OSError as e:
        if log:
            log(f"could not record the restore result: {e}")
        return False


def ack_result(install_dir):
    """Mark the last result seen. True when there was one. Raises OSError."""
    res = read_result(install_dir)
    if res is None:
        return False
    res["acknowledged"] = True
    atomic_io.write_json_atomic(result_path(install_dir), res,
                                mode=atomic_io.PRIVATE_FILE_MODE)
    return True


def _staged_at(marker):
    try:
        return float(marker.get("staged_at"))
    except (TypeError, ValueError):
        return None


def _db_moved_in(db):
    """The staged database is the live file now (by inode)."""
    try:
        return bool(db.get("staged_ino")) and _ino(db["live"]) == list(db["staged_ino"])
    except (OSError, KeyError, TypeError):
        return False


def _config_moved_in(cfg):
    """config.yaml holds the staged settings now (by content: it may have
    been written rather than renamed)."""
    try:
        return (bool(cfg.get("staged_sha256"))
                and _sha256_file(os.path.realpath(str(cfg["live"]))) == cfg["staged_sha256"])
    except (OSError, KeyError, TypeError):
        return False


def _config_switched(cfg):
    """config.yaml holds the staged settings now: renamed in (the staged
    copy is gone), or written in — across filesystems or in place — and cut
    short before the staged copy was removed (unless the staged settings
    are the very text config.yaml held before the restore: then nothing
    tells the two apart, and nothing needs to)."""
    if not cfg.get("staged") or not _config_moved_in(cfg):
        return False
    return (not _lexists(cfg["staged"])
            or cfg.get("staged_sha256") != cfg.get("snapshot_sha256"))


def _nothing_moved(marker):
    """True while no staged file has been moved into place: no swap has
    started. (A start killed part-way through a swap leaves the marker
    `staged` with something already moved in — that one must be finished,
    never dropped.) A staged file that is gone although the live file isn't
    it (deleted by hand, a cleanup tool) was never moved: that restore can
    still expire, and its rollback must keep the live file. config.yaml
    written (not renamed: another filesystem, an in-place layout) and cut
    short before its staged copy was removed already holds the restored
    settings: that counts as moved too, unless they are the very text it
    held before the restore."""
    db = marker.get("db") or {}
    if db.get("staged") and not _lexists(db["staged"]) and _db_moved_in(db):
        return False
    if _config_switched(marker.get("config") or {}):
        return False
    return True


def swap_in_progress(install_dir):
    """A start was cut short part-way through switching to a staged restore
    (the marker still says `staged`, something already moved in): main's
    load_config must then leave config.yaml as it is — not migrate or
    recover it — until apply_pending_restore has finished the swap."""
    m = read_marker(install_dir)
    return bool(m) and m.get("phase") == "staged" and not _nothing_moved(m)


def is_expired(marker, now=None):
    """A `staged` marker older than RESTORE_STAGE_TTL (or dated more than
    that in the future: a clock that jumped back must not keep it forever),
    whose swap hasn't started."""
    if not marker or marker.get("phase") != "staged" or not _nothing_moved(marker):
        return False
    at = _staged_at(marker)
    if at is None:
        return True
    age = (time.time() if now is None else now) - at
    return age > RESTORE_STAGE_TTL or age < -RESTORE_STAGE_TTL


def pending(install_dir, now=None):
    """The marker of a restore that waits for the next start (staged, not
    expired), else None. Settings writes and maintenance are refused while
    one waits."""
    m = read_marker(install_dir)
    if not m or m.get("phase") != "staged" or is_expired(m, now):
        return None
    return m


def protected_paths(install_dir):
    """Every path the marker names (staged files and pre-restore copies), in
    any phase — the pre-restore prune and purge never delete these."""
    m = read_marker(install_dir)
    out = set()
    if not m:
        return out
    for part in (m.get("db") or {}, m.get("config") or {}):
        for k in ("staged", "snapshot"):
            v = part.get(k)
            if isinstance(v, str) and v:
                out.add(os.path.abspath(v))
    return out


def status(install_dir, now=None):
    """For GET /api/maintenance/status → `restore`."""
    m = read_marker(install_dir)
    pend = None
    if m and m.get("phase") == "staged" and not is_expired(m, now):
        at = _staged_at(m)
        pend = {
            "phase": "staged", "kind": m.get("kind") or "restore",
            "staged_at": at, "expires_at": (at + RESTORE_STAGE_TTL) if at else None,
            "restart_failed": m.get("restart_failed"),
            "waiting_for_restart": m.get("staged_by_pid") == os.getpid(),
            "source": m.get("source"),
            "backup_created_at_iso": m.get("backup_created_at_iso"),
            "keep_current_config": bool(m.get("keep_current_config")),
            "notes": list(m.get("notes") or []),
            "ntfy_applied": bool(m.get("ntfy_applied")),
        }
    return {"pending": pend, "result": read_result(install_dir)}


def note_ntfy_applied(install_dir):
    """The notification server's part of a staged restore was applied at
    once (it is its own service): a cancel or an expiry must say so rather
    than "nothing was changed". Best effort."""
    m = read_marker(install_dir)
    if not m or m.get("phase") != "staged":
        return False
    m["ntfy_applied"] = True
    try:
        write_marker(install_dir, m)
        return True
    except OSError:
        return False


def note_restart_failed(install_dir, note):
    """The restart that should apply a staged restore didn't happen: record
    why (best effort) so the Settings page can say so and offer Cancel."""
    m = read_marker(install_dir)
    if not m or m.get("phase") != "staged":
        return False
    m["restart_failed"] = str(note or "the restart call failed")[:500]
    try:
        write_marker(install_dir, m)
        return True
    except OSError:
        return False


# ------------------------------------------------------- reading the zip

def _unbounded_entry(zf):
    """The first entry zipfile would decompress without a size limit, or
    None. Every size check here relies on the size an entry declares, and
    zipfile only stops at that size for stored and deflated entries: bzip2,
    LZMA and zstd entries decompress all they hold first (a few-KB zip can
    expand to gigabytes in memory). Encrypted entries can't be read at all.
    Backups are always written with deflate."""
    for i in zf.infolist():
        if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or i.flag_bits & 0x1:
            return i
    return None


def open_backup(zip_path):
    """(ZipFile, prefix, manifest). Raises RestoreError (400)."""
    try:
        zf = zipfile.ZipFile(str(zip_path), "r")
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise RestoreError(
            "Not a valid zip file — it may be incomplete. Download or copy the backup "
            f"again, then retry. Nothing was changed. ({e})")
    try:
        odd = _unbounded_entry(zf)
        if odd is not None:
            raise RestoreError(
                f"This zip stores {odd.filename[:200]} with a compression method (or "
                "encryption) Aerodrome backups never use, so it wasn't unpacked. If a "
                "cloud or archive tool re-packed the backup, zip it again with standard "
                "compression. Nothing was changed.")
        names = [n for n in zf.namelist()
                 if n == "manifest.json" or n.endswith("/manifest.json")]
        if not names:
            raise RestoreError("This zip has no manifest.json, so it isn't an Aerodrome "
                               "backup. Nothing was changed.")
        if len(names) > 1:
            raise RestoreError("This zip holds more than one backup ("
                               + ", ".join(names[:5]) + "). Restore them one at a time. "
                               "Nothing was changed.")
        info = zf.getinfo(names[0])
        if info.file_size > MAX_MANIFEST_BYTES:
            raise RestoreError("This backup's manifest.json is improbably large, so it "
                               "wasn't read. Nothing was changed.")
        try:
            import json
            manifest = json.loads(zf.read(info).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, OSError, EOFError,
                RuntimeError, NotImplementedError, RecursionError) as e:
            raise RestoreError(f"This backup's manifest.json is damaged ({e}). "
                               "Nothing was changed.")
        if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
            ver = manifest.get("manifest_version") if isinstance(manifest, dict) else "?"
            raise RestoreError(f"Unsupported backup format (manifest_version {ver!r}). This "
                               "Aerodrome restores version 1 backups. Nothing was changed.")
        return zf, names[0][: -len("manifest.json")], manifest
    except BaseException:
        zf.close()
        raise


def member(zf, name):
    try:
        return zf.getinfo(name)
    except KeyError:
        return None


def read_config_member(zf, info):
    """The backup's config.yaml text. Raises RestoreError(422) — the
    settings can't be used, but the backup's database may still be: the
    caller offers the database-only restore."""
    if info.file_size > MAX_CONFIG_BYTES:
        raise RestoreError(f"The backup's config.yaml is improbably large ({info.file_size} "
                           "bytes), so it wasn't read. Nothing was changed.", 422)
    try:
        return zf.read(info).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError, EOFError, RuntimeError,
            NotImplementedError) as e:
        raise RestoreError(f"The backup's config.yaml is damaged ({e}). Nothing was "
                           "changed.", 422)


# What a restored manifest may put into the marker, the result and the log.
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?")
_VERSION_RE = re.compile(r"[0-9A-Za-z.+-]{1,32}")


def clean_manifest_field(value, kind):
    """A manifest's created_at_iso (kind "iso") or aerodrome_version
    ("version") when it looks like one, else None — the manifest is the
    backup's text: any JSON type, any length, newlines included, and it is
    shown on the Settings page and written to the log."""
    rx = _ISO_RE if kind == "iso" else _VERSION_RE
    if isinstance(value, str) and len(value) <= 40 and rx.fullmatch(value):
        return value
    return None


# --------------------------------------------------------------- settings

def _host_set(hosts):
    """server._configured_hosts' text as the set _host_allowed compares."""
    return {p.strip().lower().rstrip(".") for p in str(hosts or "").split(",")} - {""}


def prepare_config(text, current_cfg, validate, hosts_of=None):
    """Check a backup's config.yaml the way startup reads it, keep this
    machine's web.host / web.port / data.db_file / logging.dir (with a note
    for each that differed), and run Settings' validator on the result.
    Returns (text_to_write, notes). Raises RestoreError(422) — with `errors`
    from the validator — when it couldn't run here.

    hosts_of: callable(cfg) → the hostnames the web UI answers to under cfg
    (server._configured_hosts); a note names any the backup adds."""
    try:
        # Anchors and aliases (&a / *a) are refused first: Aerodrome never
        # writes them, and a few hundred bytes of nested aliases expand to
        # billions of nodes when compared or shown (with the maintenance lock
        # held). Only incoming text is refused — a hand-edited live
        # config.yaml that uses them still starts.
        config_io.refuse_aliases(text)
        cfg = config_io.check_loadable(text)
    except ValueError as e:
        raise RestoreError(f"The backup's config.yaml couldn't be used ({e}). "
                           "Nothing was changed.", 422)
    notes, overrides = [], []
    for section, key in MACHINE_KEYS:
        mine = (current_cfg.get(section) or {}).get(key)
        theirs = cfg[section].get(key)
        if mine is not None and theirs != mine:
            overrides.append((section, key, mine))
            notes.append(f"Kept this machine's {section}.{key} ({mine}); the backup had "
                         f"{theirs if theirs is not None else 'none'}.")
    merged = _with_overrides(cfg, overrides)
    if hosts_of is not None:
        try:
            added = sorted(_host_set(hosts_of(merged)) - _host_set(hosts_of(current_cfg)))
        except Exception:
            added = []
        if added:
            notes.append("The backup's settings also let the web UI answer to these "
                         "hostnames, which this machine's didn't: " + ", ".join(added[:20])
                         + ". Check Configuration → Web server → Allowed hostnames after the "
                         "restart if you don't use them.")
    errors = validate(merged)
    if errors:
        raise RestoreError(
            "The backup's settings (config.yaml) don't pass validation, so restoring them "
            "could stop Aerodrome from starting. Nothing was changed. You can restore just "
            "the database and keep this machine's settings.", 422,
            errors=[{"path": p, "message": m} for p, m in errors])
    if overrides:
        text = _render_with_overrides(text, overrides)
        try:
            if config_io.check_loadable(text) != merged:
                raise ValueError("render differs")
        except ValueError:
            text = yaml.safe_dump(merged, default_flow_style=False, sort_keys=False,
                                  allow_unicode=True)
    return text, notes


def _with_overrides(cfg, overrides):
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    for section, key, value in overrides:
        out[section][key] = value
    return out


def _render_with_overrides(text, overrides):
    """Apply the machine-key overrides keeping the backup's comments when
    ruamel is there (the caller checks the result reads back the same)."""
    try:
        from io import StringIO
        from ruamel.yaml import YAML
        ry = YAML()
        ry.preserve_quotes = True
        ry.indent(mapping=2, sequence=4, offset=2)
        doc = ry.load(text)
        for section, key, value in overrides:
            doc[section][key] = value
        buf = StringIO()
        ry.dump(doc, buf)
        return buf.getvalue()
    except Exception:
        cfg = yaml.safe_load(text)
        for section, key, value in overrides:
            cfg[section][key] = value
        return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False,
                              allow_unicode=True)


# ---------------------------------------------------------------- database

def _open_new(dest, mode):
    """Create `dest` afresh (a leftover of an earlier attempt is removed
    first), never through a symlink, with permission bits `mode`."""
    _unlink(dest)
    for s in _sidecars(dest):
        _unlink(s)
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.fchmod(fd, mode)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _disk_full_error():
    return RestoreError("The disk filled up while the backup was being unpacked. Nothing "
                        "was changed — free some space and try again.", 507)


def stage_database(zf, info, dest, mode=0o644):
    """Stream the zip's database to `dest` (beside the live database, so the
    swap is a rename). CRC errors, short reads and a full disk raise
    RestoreError and leave no staging file. Returns the bytes written."""
    written = 0
    try:
        with zf.open(info) as src, _open_new(dest, mode) as out:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        if written != info.file_size:
            raise RestoreError(f"The database in this backup is cut short ({written} of "
                               f"{info.file_size} bytes). Nothing was changed.")
    except RestoreError:
        _unlink(dest)
        raise
    except (zipfile.BadZipFile, EOFError, ValueError, OSError, RuntimeError) as e:
        _unlink(dest)
        if isinstance(e, OSError) and atomic_io.is_disk_full(e):
            raise _disk_full_error()
        raise RestoreError(f"The database inside this backup is damaged ({e}). Nothing "
                           "was changed.")
    return written


def copy_database(src, dest, mode=0o644):
    """Copy a pre-restore copy (an ordinary database file nothing writes) to
    `dest` with the SQLite backup API, reading it as an immutable file — it
    is never opened for writing, never aliased. Raises RestoreError."""
    src = Path(src)
    if not src.is_file():
        raise RestoreError(f"The copy {src.name} is gone. Nothing was changed.", 404,
                           code="not_found")
    try:
        _open_new(dest, mode).close()
        # as_uri() percent-encodes '#', '?' and '%': unencoded, a folder
        # named "a#b" opened ".../a" read-write (the flags fell into the
        # URI's fragment) and created a stray empty file there.
        s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
                            timeout=30)
        try:
            d = sqlite3.connect(str(dest), timeout=30)
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        for x in _sidecars(dest):
            _unlink(x)
        fd = os.open(str(dest), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except sqlite3.Error as e:
        _cleanup_db(dest)
        if "full" in str(e).lower():
            raise _disk_full_error()
        raise RestoreError(f"The copy {src.name} can't be read as a database ({e}). "
                           "Nothing was changed.")
    except OSError as e:
        _cleanup_db(dest)
        if atomic_io.is_disk_full(e):
            raise _disk_full_error()
        raise


def _cleanup_db(p):
    _unlink(p)
    for s in _sidecars(p):
        _unlink(s)


def check_database(path, current_schema_version):
    """SQLite magic, quick_check(5), the required tables, and a schema no
    newer than this release understands. Returns the backup's schema
    version. Raises RestoreError. Leaves no -wal/-shm behind."""
    try:
        with open(str(path), "rb") as f:
            magic = f.read(16)
    except OSError as e:
        raise RestoreError(f"The restored database couldn't be read ({e}). Nothing was "
                           "changed.")
    if magic != SQLITE_MAGIC:
        raise RestoreError("The backup's aerodrome.db isn't a SQLite database. Nothing was "
                           "changed.")
    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        conn.execute("PRAGMA query_only = 1")
        rows = conn.execute("PRAGMA quick_check(5)").fetchall()
        if [r[0] for r in rows] != ["ok"]:
            raise RestoreError("The database in this backup is damaged (quick_check: "
                               + "; ".join(str(r[0])[:200] for r in rows)
                               + "). Nothing was changed.")
        names = ("schema_version",) + REQUIRED_TABLES
        kinds = {}
        for name, typ in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE name IN (%s)"
                % ",".join("?" * len(names)), names):
            kinds.setdefault(name, set()).add(typ)
        # A view (or index, trigger) under one of these names is never
        # Aerodrome's: a view named schema_version answered "v999" to the
        # migrations — and nothing to the check below.
        odd = sorted(n for n, t in kinds.items() if t != {"table"})
        if odd:
            raise RestoreError("The backup's database isn't an Aerodrome history database "
                               f"({', '.join(odd)} isn't a table). Nothing was changed.")
        missing = [t for t in REQUIRED_TABLES if t not in kinds]
        if missing:
            raise RestoreError("The backup's database isn't an Aerodrome history database "
                               f"(it has no {', '.join(missing)}). Nothing was changed.")
        version = 0
        if "schema_version" in kinds:
            raw = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            try:
                version = int(raw or 0)
                if isinstance(raw, float) and raw != version:
                    raise ValueError(raw)
            except (TypeError, ValueError, OverflowError):
                raise RestoreError("The backup's database isn't an Aerodrome history "
                                   f"database (its schema version is {str(raw)[:40]!r}). "
                                   "Nothing was changed.")
        if version > current_schema_version:
            raise RestoreError(
                f"This backup was made by a newer Aerodrome (database schema v{version}; this "
                f"one understands up to v{current_schema_version}). Update Aerodrome first, "
                "then restore. Nothing was changed.", 409)
        return version
    except sqlite3.Error as e:
        if "full" in str(e).lower():
            raise _disk_full_error()
        raise RestoreError(f"The database in this backup can't be opened ({e}). Nothing "
                           "was changed.")
    finally:
        if conn is not None:
            conn.close()
        for s in _sidecars(path):
            _unlink(s)


def snapshot_database(live, dest, mode=None):
    """A consistent copy of the live database through the SQLite backup API
    (commits still in -wal included, never torn by a concurrent
    checkpoint), written to a temp name and renamed when complete. None when
    there is no live database. Raises RestoreError(507) on a full disk."""
    live, dest = Path(live), Path(dest)
    if not live.is_file():
        return None
    tmp = dest.with_name(f".{dest.name}.tmp")
    try:
        _open_new(tmp, mode if mode is not None else 0o644).close()
        src = sqlite3.connect(str(live), timeout=60)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        for s in _sidecars(tmp):
            _unlink(s)
        fd = os.open(str(tmp), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(dest))
        atomic_io.fsync_dir(dest.parent)
        return dest
    except sqlite3.Error as e:
        _cleanup_db(tmp)
        if "full" in str(e).lower():
            raise RestoreError("Not enough free disk space to keep a copy of your current "
                               "database. Nothing was changed.", 507)
        raise RestoreError(f"Your current database couldn't be copied ({e}). Nothing was "
                           "changed.", 500, code="snapshot_failed")
    except OSError as e:
        _cleanup_db(tmp)
        if atomic_io.is_disk_full(e):
            raise RestoreError("Not enough free disk space to keep a copy of your current "
                               "database. Nothing was changed.", 507)
        raise


def _mode_of(p, default):
    try:
        return os.stat(str(p)).st_mode & 0o777
    except OSError:
        return default


def _free_ts(db_live, config_live, ts):
    """`ts`, moved on a second at a time until neither pre-restore name is
    taken (two restores in the same second)."""
    t = time.mktime(time.strptime(ts, "%Y%m%d-%H%M%S"))
    for _ in range(120):
        cand = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
        if not (_lexists(db_snapshot_path(db_live, cand))
                or _lexists(config_snapshot_path(config_live, cand))):
            return cand
        t += 1
    raise RestoreError("Too many restores in a row — try again in a minute.", 409,
                       code="busy")


# -------------------------------------------------------- preparing (server)

def prepare(install_dir, *, db_live, config_live, source, manifest, notes,
            cfg_text=None, db_writer=None, db_size=0, current_schema=0,
            keep_current_config=False, kind="restore", ts=None, now=None, free_bytes=None):
    """Stage a restore and write the marker; nothing live changes.

    cfg_text: the settings to switch to (already checked by prepare_config),
              or None to keep config.yaml as it is.
    db_writer: callable(dest, mode) writing the database to switch to into
              `dest` (RestoreError on a damaged source), or None.
    db_size:  the database's unpacked size, for the space check.
    Returns (marker, info): info says what is staged and which copies were
    kept. Raises RestoreError with everything it made removed again.

    db_live is resolved to its real path: with data.db_file a symlink to
    another disk, the staged file, the copy, the space check and the swap
    are all on the database's own filesystem, and the swap renames onto the
    real file (the link survives). The marker keeps both paths."""
    install_dir = Path(install_dir)
    db_configured = Path(db_live)
    db_live = Path(os.path.realpath(str(db_live)))
    config_live = Path(config_live)
    if cfg_text is None and db_writer is None:
        raise RestoreError("Nothing to restore: this backup holds neither settings nor a "
                           "database. Nothing was changed.", code="nothing_to_restore")
    now = time.time() if now is None else now
    ts = _free_ts(db_live, config_live, ts or time.strftime("%Y%m%d-%H%M%S",
                                                            time.localtime(now)))
    made = []
    info = {}
    try:
        db_part = {}
        if db_writer is not None:
            # Room on the database's own filesystem for the restored copy and
            # a copy of the current database (with its -wal), plus headroom.
            live_size = sum(_size_if_file(p) for p in (db_live, Path(f"{db_live}-wal")))
            try:
                free = (free_bytes if free_bytes is not None
                        else _disk_free(db_live.parent))
            except OSError:
                free = None
            need = int(db_size or 0) + live_size + HEADROOM_BYTES
            if free is not None and free < need:
                mb = 1024 * 1024
                raise RestoreError(
                    f"Not enough free disk space to restore: it needs about {need // mb} MB "
                    f"(the restored database, {int(db_size or 0) // mb} MB, a copy of your "
                    f"current one and some headroom); {free // mb} MB available. Nothing "
                    "was changed.", 507)
            mode = _mode_of(db_live, 0o644)
            staged = staged_db_path(db_live)
            made.append(staged)
            db_writer(staged, mode)
            schema_v = check_database(staged, current_schema)
            db_part = {"live": str(db_live), "staged": str(staged),
                       "staged_ino": _ino(staged), "snapshot": None, "snapshot_ino": None,
                       "schema_version": schema_v}
            if str(db_configured) != str(db_live):
                db_part["configured"] = str(db_configured)
            info["database"] = {"ok": True, "bytes": os.stat(str(staged)).st_size,
                                "schema_version": schema_v}
        cfg_part = {}
        if cfg_text is not None:
            config_staged = staged_config_path(config_live)
            made.append(config_staged)
            config_io.write_text(config_staged, cfg_text, check=True)
            cfg_part = {"live": str(config_live), "staged": str(config_staged),
                        "staged_sha256": _sha256_file(config_staged),
                        "snapshot": None, "snapshot_ino": None, "snapshot_sha256": None}
            info["config"] = {"ok": True, "bytes": len(cfg_text.encode("utf-8"))}
        # The undo copies: the current database (backup API — complete even
        # while the collector writes), beside it, with its permission bits.
        if db_part:
            snap = db_snapshot_path(db_live, ts)
            made.append(snap)
            got = snapshot_database(db_live, snap, mode=_mode_of(db_live, 0o644))
            if got is not None:
                db_part["snapshot"] = str(got)
                db_part["snapshot_ino"] = _ino(got)
            info["database"]["previous_backed_up_as"] = got.name if got else None
        marker = {
            "version": MARKER_VERSION, "kind": kind, "phase": "staged",
            "staged_at": now, "staged_by_pid": os.getpid(), "ts": ts, "source": source,
            "backup_created_at_iso": clean_manifest_field(
                (manifest or {}).get("created_at_iso"), "iso"),
            "backup_aerodrome_version": clean_manifest_field(
                (manifest or {}).get("aerodrome_version"), "version"),
            "notes": list(notes or []), "keep_current_config": bool(keep_current_config),
            "db": db_part, "config": cfg_part, "restart_failed": None,
        }
        # config.yaml's copy and the marker together, under the config lock:
        # a Settings save can't land between them (it is refused once the
        # marker exists, and the copy is the file it would have changed).
        with config_io.CONFIG_LOCK:
            if cfg_part and config_live.is_file():
                config_snapshot = config_snapshot_path(config_live, ts)
                made.append(config_snapshot)
                config_io.write_text(config_snapshot,
                                     Path(os.path.realpath(str(config_live)))
                                     .read_text(encoding="utf-8"), check=False)
                cfg_part["snapshot"] = str(config_snapshot)
                cfg_part["snapshot_ino"] = _ino(config_snapshot)
                cfg_part["snapshot_sha256"] = _sha256_file(config_snapshot)
                info["config"]["previous_backed_up_as"] = config_snapshot.name
            try:
                write_marker(install_dir, marker)
            except OSError as e:
                if atomic_io.is_disk_full(e):
                    raise RestoreError("The disk is full, so the restore couldn't be "
                                       "recorded. Nothing was changed.", 507)
                raise
        return marker, info
    except config_io.ConfigWriteError as e:
        _discard(made)
        raise RestoreError(str(e), 507 if e.disk_full else 500,
                           code="no_space" if e.disk_full else "write_failed")
    except BaseException:
        _discard(made)
        raise


def _disk_free(d):
    import shutil
    return shutil.disk_usage(str(d)).free


def _discard(paths):
    for p in paths:
        _unlink(p)
        for s in _sidecars(p):
            _unlink(s)
        _unlink(Path(p).with_name(f".{Path(p).name}.tmp"))


# ------------------------------------------------- the startup half (main)

def _staged_files(marker):
    out = []
    db = marker.get("db") or {}
    if db.get("staged"):
        out.append(Path(db["staged"]))
        out += _sidecars(db["staged"])
    cfg = marker.get("config") or {}
    if cfg.get("staged"):
        out.append(Path(cfg["staged"]))
    return out


_DROP_SEQ = itertools.count(1)
DROPPING_INFIX = ".dropping-"


def _same_restore(a, b):
    return all(a.get(k) == b.get(k) for k in ("staged_at", "ts", "staged_by_pid"))


def _drop(install_dir, marker, result, log):
    """End a restore that never switched anything (cancelled, expired).

    The marker is CLAIMED first — renamed to a name of this call's own —
    and checked to be the restore that was read: a new restore staged
    meanwhile under the same fixed names is never dropped by a caller still
    working on the old one (the drop is also only ever run under the
    maintenance lock, or at startup). Then the result (best effort), then
    the claimed marker goes — the restore is over — then the staged files,
    each only when it is still the file this restore staged (inode for the
    database, content for the settings). The next start sweeps whatever is
    left. Returns False when the marker was no longer this restore's."""
    mp = marker_path(install_dir)
    claimed = mp.with_name(f"{MARKER_NAME}{DROPPING_INFIX}{os.getpid()}-{next(_DROP_SEQ)}")
    try:
        os.rename(str(mp), str(claimed))
    except FileNotFoundError:
        return False
    got = atomic_io.read_json(claimed)
    if not isinstance(got, dict) or not _same_restore(got, marker):
        # Not the restore that was read: put it back unless a newer one is
        # there already (link: never over an existing name), and stop.
        try:
            os.link(str(claimed), str(mp))
        except OSError:
            pass
        _unlink(claimed)
        return False
    write_result(install_dir, result, log)
    _unlink(claimed)
    atomic_io.fsync_dir(mp.parent)
    db = marker.get("db") or {}
    cfg = marker.get("config") or {}
    victims = []
    if db.get("staged") and _lexists(db["staged"]):
        try:
            if not db.get("staged_ino") or _ino(db["staged"]) == list(db["staged_ino"]):
                victims += [Path(db["staged"])] + _sidecars(db["staged"])
        except OSError:
            pass
    if cfg.get("staged") and _lexists(cfg["staged"]):
        try:
            if (not cfg.get("staged_sha256")
                    or _sha256_file(cfg["staged"]) == cfg["staged_sha256"]):
                victims.append(Path(cfg["staged"]))
        except OSError:
            pass
    for p in victims:
        try:
            _unlink(p)
        except OSError as e:
            log(f"could not remove {p}: {e}")
    return True


NTFY_KEPT_NOTE = ("The notification server's settings from the backup were already "
                  "applied and stay; nothing else was changed.")


def _dropped_result(marker, **what):
    res = {"ok": False, **what, "staged_at": _staged_at(marker),
           "source": marker.get("source"), "kind": marker.get("kind") or "restore",
           "backup_created_at_iso": marker.get("backup_created_at_iso"),
           "at": time.time(), "acknowledged": False}
    if marker.get("ntfy_applied"):
        res["ntfy_applied"] = True
    return res


def _nothing_changed(marker):
    return NTFY_KEPT_NOTE if marker.get("ntfy_applied") else "nothing was changed."


def discard_if_expired(install_dir, log=print, now=None):
    """A staged restore past RESTORE_STAGE_TTL is dropped (nothing live was
    changed); the result says so. True when one was dropped. The server
    calls it only with the maintenance lock held (or startup does)."""
    m = read_marker(install_dir)
    if not m or not is_expired(m, now):
        return False
    if not _drop(install_dir, m, _dropped_result(m, expired=True), log):
        return False
    log(f"A restore staged at {time.ctime(_staged_at(m) or 0)} wasn't applied within "
        f"{RESTORE_STAGE_TTL // 60} minutes; it was dropped — {_nothing_changed(m)}")
    return True


def cancel_pending_restore(install_dir, log=print):
    """Cancel a restore that waits for its restart (phase staged only).
    Returns the marker that was cancelled, or None when nothing waits.
    Raises ValueError for a restore already being switched in (a swap that
    was cut short part-way counts: the next start finishes it)."""
    m = read_marker(install_dir)
    if not m:
        return None
    if m.get("phase") != "staged" or not _nothing_moved(m):
        raise ValueError(m.get("phase"))
    if not _drop(install_dir, m, _dropped_result(m, cancelled=True), log):
        return None
    log(f"A staged restore was cancelled; {_nothing_changed(m)}")
    return m


def _swap_in(marker):
    """staged → files in place. Each step is idempotent (inode / content
    checks), so a replay after a kill anywhere finishes it."""
    db = marker.get("db") or {}
    if db.get("staged"):
        staged, live = Path(db["staged"]), Path(db["live"])
        if _lexists(staged):
            for s in _sidecars(live):      # the old database's journal must never replay
                _unlink(s)
            os.replace(str(staged), str(live))
            atomic_io.fsync_dir(live.parent)
        elif not (live.exists() and db.get("staged_ino") and _ino(live) == list(db["staged_ino"])):
            raise ApplyError(f"the staged database {staged.name} is gone")
    cfg = marker.get("config") or {}
    if cfg.get("staged"):
        config_staged, config_live = Path(cfg["staged"]), Path(cfg["live"])
        try:
            config_io.move_into_place(config_staged, config_live)
        except FileNotFoundError:
            if _sha256_file(os.path.realpath(str(config_live))) != cfg.get("staged_sha256"):
                raise ApplyError(f"the staged settings {config_staged.name} are gone")


def finish_config_swap(install_dir, log=print):
    """main.load_config found config.yaml unreadable while a restore is
    staged and a start had begun switching to it (the marker's
    swap_started, written before the first file moves — or a file already
    moved in): an in-place write of the staged settings was cut short,
    leaving a mix. The complete staged settings are right beside it: put
    them in place (idempotent — the swap's replay then finds them there)
    instead of recovering an older copy over them. True when it did.

    A config.yaml that is unreadable for any other reason while a restore
    waits (broken by hand; the restore expired or not, an update being
    confirmed or not) is not a swap cut short: it goes to the usual config
    recovery, and the start then applies, defers or drops the restore as it
    would anyway."""
    m = read_marker(install_dir)
    if not m or m.get("phase") != "staged":
        return False
    cfg = m.get("config") or {}
    staged = cfg.get("staged")
    if not staged or not cfg.get("live") or not _lexists(staged):
        return False
    if not m.get("swap_started") and _nothing_moved(m):
        return False
    try:
        config_io.move_into_place(Path(staged), Path(cfg["live"]))
    except (OSError, config_io.ConfigWriteError) as e:
        log(f"Couldn't finish switching to the restored settings ({e}).")
        return False
    log("Finished switching to the restored settings (a start before this one was cut "
        "short while writing them).")
    return True


def _rollback_cut_short(marker):
    """A rollback started and was cut short before its phase could be
    recorded (the disk was full): a pre-restore copy is already back — by
    inode (renamed back), or for config.yaml by content (written back
    across filesystems or in place)."""
    db = marker.get("db") or {}
    if db.get("snapshot") and not _lexists(db["snapshot"]) and db.get("snapshot_ino"):
        try:
            if _ino(db["live"]) == list(db["snapshot_ino"]):
                return True
        except OSError:
            pass
    cfg = marker.get("config") or {}
    if cfg.get("snapshot") and not _lexists(cfg["snapshot"]):
        real = os.path.realpath(str(cfg.get("live")))
        try:
            if cfg.get("snapshot_ino") and _ino(real) == list(cfg["snapshot_ino"]):
                return True
            if cfg.get("snapshot_sha256") and _sha256_file(real) == cfg["snapshot_sha256"]:
                return True
        except OSError:
            pass
    return False


def apply_pending_restore(install_dir, log=print, update_pending=False, now=None):
    """The start's half. Returns (state, marker):

      "none"        no marker (an unreadable one is set aside)
      "expired"     a staged restore older than RESTORE_STAGE_TTL was dropped
      "deferred"    staged, but an update is still being confirmed: the update
                    goes first (if it is rolled back, the release before it
                    must start on the data it knows); the restore waits
      "applied"     staged files moved into place (marker now `swapped`)
      "retry"       swapped already; a start that died before finishing — one
                    retry (marker counts it)
      "rolled_back" the previous data was put back (marker gone, result kept)
      "unknown"     a phase this release doesn't know: left alone

    Must run before any database connection exists. Raises on a failure
    part-way; the caller then rolls back (rollback_pending_restore)."""
    install_dir = Path(install_dir)
    if not marker_exists(install_dir):
        return "none", None
    m = read_marker(install_dir)
    if m is None:
        _set_aside_unreadable(install_dir, log)
        return "none", None
    phase = m.get("phase")
    if phase not in PHASES:
        log(f"The restore marker has a phase this release doesn't know ({phase!r}); "
            f"leaving it alone.")
        return "unknown", m
    if phase == "staged":
        # (Before the expiry: a rollback cut short before it could record
        # its phase has put a copy back already — it is finished and
        # reported, never mistaken for a restore that never started.)
        if _rollback_cut_short(m):
            rollback_pending_restore(install_dir, m, m.get("error") or
                                     "an interrupted rollback was finished", log)
            return "rolled_back", m
        if is_expired(m, now):
            discard_if_expired(install_dir, log, now)
            return "expired", m
        if update_pending and _nothing_moved(m):
            # (Only before anything moved: a swap cut short part-way is
            # finished — the data already changed, so waiting protects
            # nothing — and the update confirms after it.)
            log("A restore is staged, but the last update is still being confirmed: it "
                "goes first. The restore applies at a start after that (within an hour of "
                "staging it), or can be cancelled in Settings.")
            return "deferred", m
        if not m.get("swap_started"):
            # Recorded before the first file moves: a start cut short inside
            # an in-place write of the restored settings leaves config.yaml
            # unreadable, and only this tells the next start that it was the
            # swap's own write (finish_config_swap finishes it) rather than a
            # hand edit. Best effort: a disk too full for it is too full for
            # an in-place write as well (config_io refuses that one first),
            # and the renames need no space.
            m["swap_started"] = time.time()
            try:
                write_marker(install_dir, m)
            except OSError as e:
                log(f"could not record that the switch started ({e}); switching anyway")
        _swap_in(m)
        m["phase"] = "swapped"
        m["starts"] = 1
        m["swapped_at"] = time.time()
        write_marker(install_dir, m)
        log(f"Switched to the restored backup ({m.get('source') or 'backup'}, made "
            f"{m.get('backup_created_at_iso') or 'at an unknown time'}).")
        return "applied", m
    if phase == "swapped":
        if _rollback_cut_short(m):
            rollback_pending_restore(install_dir, m, m.get("error") or
                                     "an interrupted rollback was finished", log)
            return "rolled_back", m
        starts = _starts(m) + 1
        if starts > MAX_SWAPPED_STARTS:
            err = m.get("error") or (f"Aerodrome didn't finish starting on the restored "
                                     f"backup ({starts - 1} attempts)")
            rollback_pending_restore(install_dir, m, err, log)
            return "rolled_back", m
        m["starts"] = starts
        write_marker(install_dir, m)       # can't count it (disk full) → the caller rolls back
        return "retry", m
    # phase == "rollback"
    rollback_pending_restore(install_dir, m, m.get("error") or "unknown error", log)
    return "rolled_back", m


def _starts(marker):
    """The starts counted so far on the restored files. 0 is a real count
    (every start so far waited for the web address, note_bind_wait) — not
    "missing", which only a marker from before the count existed would be."""
    s = marker.get("starts")
    return 1 if s is None else int(s)


def note_bind_wait(install_dir, marker, log=print):
    """A start on the restored files couldn't listen on the web address,
    which a restore never changes: that start doesn't count towards
    MAX_SWAPPED_STARTS (a rollback wouldn't make the address usable). The
    restore stays switched in and finishes at the first start that can
    listen. Best effort."""
    m = read_marker(install_dir)
    if not m or m.get("phase") != "swapped" or not _same_restore(m, marker):
        return False
    m["starts"] = max(0, _starts(m) - 1)
    m["bind_waits"] = int(m.get("bind_waits") or 0) + 1
    try:
        write_marker(install_dir, m)
        return True
    except (OSError, ValueError, TypeError) as e:
        log(f"could not record the wait for the web address: {e}")
        return False


def _set_aside_unreadable(install_dir, log):
    set_aside_marker(install_dir, log, "couldn't be read")


def set_aside_marker(install_dir, log=print, why="couldn't be used"):
    """Move the marker out of the way (kept in .backups/ for a look), so a
    marker no release can act on never stops a start."""
    p = marker_path(install_dir)
    dest = p.with_name(f"{MARKER_NAME}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(str(p), str(dest))
        log(f"The restore marker {why}; it was set aside as .backups/{dest.name} "
            f"and nothing was restored.")
        return True
    except OSError as e:
        log(f"The restore marker {why} and couldn't be set aside ({e}).")
        return False


def request_rollback(install_dir, marker, error):
    """Called by a start that already opened the restored database (threads,
    -wal): it must not move files under its own connections, so it records
    the rollback — durably, it decides what the next start does; raises
    OSError — and re-execs; the next start's apply_pending_restore moves
    the copies back before any connection exists."""
    m = dict(marker)
    m["phase"] = "rollback"
    m["error"] = str(error)[:2000]
    write_marker(install_dir, m)
    return m


def _put_back_db(db, log):
    """Put the database's pre-restore copy back over the RESTORED database
    (consumes the copy). Returns "put_back", "already" (an interrupted run
    did it, or there is nothing to put back) or "kept": the restored file
    was never moved in (the swap failed before its rename, or the staged
    file vanished) — the live database is the user's own, newer than the
    copy, and stays. Raises SnapshotMissing when the restored database is
    in place (or was set aside by an interrupted run) and the copy is gone.

    "The restored file" is decided by inode (the marker's staged_ino):
    SQLite writes a database in place, so it keeps its inode while it
    runs. (On a filesystem without stable inode numbers — some FUSE or CIFS
    mounts — a restored database would be kept rather than replaced: the
    swap relies on the same check.)"""
    live = Path(db["live"])
    snap = db.get("snapshot")
    restored_here = _db_moved_in(db)
    if snap is None:
        # There was no database before the restore: remove the restored one.
        if restored_here:
            _cleanup_db(live)
            atomic_io.fsync_dir(live.parent)
            return "put_back"
        return "already"
    if restored_here:
        if not _lexists(snap):
            raise SnapshotMissing(str(snap))
        for s in _sidecars(live):           # the restored database's journal must not replay
            _unlink(s)
        os.replace(str(snap), str(live))
        atomic_io.fsync_dir(live.parent)
        return "put_back"
    if not _lexists(snap):
        try:
            if db.get("snapshot_ino") and _ino(live) == list(db["snapshot_ino"]):
                return "already"            # put back by an interrupted run
        except OSError:
            pass
        if not live.exists():
            raise SnapshotMissing(str(snap))   # set aside by an interrupted run
    return "kept"


def _set_aside_restored_db(install_dir, m, db, log):
    """The pre-restore copy is gone and the restored database didn't load:
    move the restored one aside (kept for the user) so the next start runs
    on a fresh, empty database instead of failing on it again and again.
    The name is recorded in the marker first, so a run cut short after the
    move still reports where it went."""
    live = Path(db["live"])
    rec = db.get("set_aside")
    if not live.exists():
        if rec and _lexists(live.with_name(rec)):
            dest = live.with_name(rec)
            for s in _sidecars(live):           # moved after the file: finish that too
                if _lexists(s):
                    try:
                        os.replace(str(s), f"{dest}{str(s)[len(str(live)):]}")
                    except OSError:
                        _unlink(s)
            return rec
        return None
    if not _db_moved_in(db):
        return None                         # never the user's own database
    dest = live.with_name(f"{live.name}.restore-failed.{time.strftime('%Y%m%d-%H%M%S')}")
    db["set_aside"] = dest.name
    try:
        write_marker(install_dir, m)
    except OSError as e:
        log(f"could not record where the restored database goes ({e}); moving it anyway")
    os.replace(str(live), str(dest))
    for s in _sidecars(live):
        if _lexists(s):
            try:
                os.replace(str(s), f"{dest}{str(s)[len(str(live)):]}")
            except OSError:
                _unlink(s)
    atomic_io.fsync_dir(live.parent)
    log(f"The restored database was moved aside as {dest.name}.")
    return dest.name


def _put_back_config(cfg):
    snap = cfg.get("snapshot")
    if not snap:
        return
    config_live = Path(cfg["live"])
    try:
        config_io.move_into_place(Path(snap), config_live)
        return
    except FileNotFoundError:
        pass
    real = os.path.realpath(str(config_live))
    try:
        if cfg.get("snapshot_ino") and _ino(real) == list(cfg["snapshot_ino"]):
            return                          # renamed back by an interrupted run
        if cfg.get("snapshot_sha256") and _sha256_file(real) == cfg["snapshot_sha256"]:
            return                          # written back by an interrupted run
    except OSError:
        pass
    raise SnapshotMissing(str(snap))


def _finish_rollback(install_dir, m, res, log):
    """Record the outcome, then end the restore (marker, then any staged file
    a swap never moved in). When the outcome can't be recorded (a full
    disk), the marker is KEPT — as `rollback` with the files already put
    back and the result to write — and so are the staged files: the next
    start that has room records it, so the user is told. True when ended."""
    if not write_result(install_dir, res, log):
        try:
            write_marker(install_dir, dict(m, phase="rollback", files_done=True, result=res))
        except OSError:
            pass                            # the replay redoes the (idempotent) rollback
        log("The rollback's outcome couldn't be recorded (is the disk full?); the next "
            "start records it.")
        return False
    _unlink(marker_path(install_dir))
    atomic_io.fsync_dir(marker_path(install_dir).parent)
    for p in _staged_files(m):              # anything a swap never moved in (after the
        try:                                # marker: the next start sweeps any left)
            _unlink(p)
        except OSError:
            pass
    return True


def rollback_pending_restore(install_dir, marker, error, log=print):
    """Put the pre-restore copies back. Idempotent: a kill anywhere inside,
    followed by the next start, finishes it. The phase is recorded first
    (best effort: on a full disk the rollback runs anyway — renames need no
    space — and a replay recognises the copies already moved). The marker
    is removed only once the result is recorded (_finish_rollback). A copy
    that is really gone is reported (never "rolled back"); the restored
    database is then moved aside so Aerodrome can start on a fresh one. A
    live database the swap never replaced is kept, never overwritten with
    the older copy. Returns the result."""
    m = dict(marker)
    if m.get("files_done") and isinstance(m.get("result"), dict):
        res = dict(m["result"])
        if _finish_rollback(install_dir, m, res, log):
            log("Recorded the outcome of the last restore's rollback.")
        return res
    if m.get("phase") != "rollback":
        try:
            m = request_rollback(install_dir, m, error)
        except OSError as e:
            log(f"could not record the rollback before starting it ({e}); rolling back anyway")
    db = dict(m.get("db") or {})
    m["db"] = db
    cfg = m.get("config") or {}
    missing, kept, set_aside = [], [], None
    # A staged file that is still there was never moved in (the swap failed
    # before it): the live file is the one from before the restore, newer
    # than its pre-restore copy — keep it, and keep the copy listed. The
    # staged files are removed only after the marker, so a replay sees the
    # same thing.
    if db.get("live") and "snapshot" in db:
        if db.get("staged") and _lexists(db["staged"]):
            kept.append(Path(db["live"]).name)
        else:
            try:
                if _put_back_db(db, log) == "kept":
                    kept.append(Path(db["live"]).name)
            except SnapshotMissing as e:
                missing.append(Path(str(e)).name)
                set_aside = _set_aside_restored_db(install_dir, m, db, log)
    # (config.yaml: likewise — unless its staged copy is still there only
    # because a write of it in place was cut short before removing it)
    if (cfg.get("live") and cfg.get("snapshot")
            and (not (cfg.get("staged") and _lexists(cfg["staged"]))
                 or _config_switched(cfg))):
        try:
            _put_back_config(cfg)
        except SnapshotMissing as e:
            missing.append(Path(str(e)).name)
    res = {"ok": False, "rolled_back": not missing, "at": time.time(),
           "error": str(error)[:2000], "source": m.get("source"),
           "kind": m.get("kind") or "restore",
           "backup_created_at_iso": m.get("backup_created_at_iso"),
           "staged_at": _staged_at(m), "acknowledged": False}
    if kept:
        res["kept_current"] = kept
    if missing:
        res["missing"] = missing
        if set_aside:
            res["set_aside"] = set_aside
        res["manual"] = (
            ("The restored database was moved aside as " + set_aside + " and Aerodrome "
             "started with an empty database. " if set_aside else "")
            + "Restore another backup from Settings → Backup & Restore, or stop Aerodrome "
            "and move the database file you want back into place.")
    _finish_rollback(install_dir, m, res, log)
    if missing:
        log(f"Restore could NOT be rolled back — the restored files didn't load ({error}), "
            f"and the copy taken before the restore is gone ({', '.join(missing)}).")
    elif kept:
        log(f"Restore not applied ({error}); the database it would have replaced was never "
            f"replaced and is kept as it is.")
    else:
        log(f"Restore rolled back — the restored files didn't load ({error}); the "
            f"previous data was put back.")
    return res


def finish_pending_restore(install_dir, marker, log=print):
    """The restored data started: record success (best effort), then remove
    the marker. Returns the result."""
    res = {"ok": True, "at": time.time(), "source": marker.get("source"),
           "kind": marker.get("kind") or "restore",
           "backup_created_at_iso": marker.get("backup_created_at_iso"),
           "notes": list(marker.get("notes") or []),
           "keep_current_config": bool(marker.get("keep_current_config")),
           "acknowledged": False}
    write_result(install_dir, res, log)
    _unlink(marker_path(install_dir))
    atomic_io.fsync_dir(marker_path(install_dir).parent)
    log("Restore complete — running on the restored backup.")
    return res


_STAGED_TMP_RE = re.compile(r"^\.\.config\.yaml\.restore-staged\.[a-z0-9_]{8}\.tmp\Z")


def sweep_orphans(install_dir, db_live, config_live, log=print):
    """Startup with no marker: remove what a restore cut short (killed
    before its marker was written) or a cancel left behind — the staged
    database (+ -wal/-shm/-journal), the staged config and their temp files,
    a pre-restore copy's unfinished temp file, and a marker a cancel or an
    expiry had claimed. Never a pre-restore copy or anything a marker
    names. db_live: data.db_file (resolved here: the staged file sits beside
    the real database). Returns (count, bytes)."""
    if marker_exists(install_dir):
        return 0, 0
    victims = []
    db_live = Path(os.path.realpath(str(db_live)))
    sdb = staged_db_path(db_live)
    victims += [p for p in [sdb] + _sidecars(sdb) if _lexists(p)]
    snap_tmp = re.compile(r"^\." + re.escape(db_live.name)
                          + r"\.bak\.[0-9]{8}-[0-9]{6}\.pre-restore\.tmp(-wal|-shm|-journal)?\Z")
    try:
        victims += [p for p in db_live.parent.iterdir() if snap_tmp.match(p.name)]
    except OSError:
        pass
    config_live = Path(config_live)
    scfg = staged_config_path(config_live)
    if _lexists(scfg):
        victims.append(scfg)
    try:
        victims += [p for p in config_live.parent.iterdir() if _STAGED_TMP_RE.match(p.name)]
    except OSError:
        pass
    try:
        victims += [p for p in marker_path(install_dir).parent.iterdir()
                    if p.name.startswith(MARKER_NAME + DROPPING_INFIX)]
    except OSError:
        pass
    count = freed = 0
    for p in victims:
        try:
            size = os.lstat(str(p)).st_size
            os.unlink(str(p))
            count += 1
            freed += size
        except OSError as e:
            log(f"Startup cleanup: could not remove {p}: {e}")
    if count:
        log(f"Startup cleanup: removed {count} file(s) a restore left unfinished "
            f"({freed} bytes)")
    return count, freed
