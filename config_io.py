"""config_io.py — the one way Aerodrome writes config.yaml.

Every writer used to open config.yaml for writing (which empties it first)
and then write: a full disk, a crash, or a second writer part-way through
left it empty or cut short, and main.py then refused to start — systemd
restarted it forever and the web UI, the only recovery tool most installs
have, was gone. Now every write goes:

    render → check it loads the way main.py loads it → temp file beside it
    → fsync → rename over it → fsync the directory

so a reader, the next start, or a power cut sees the old file or the new
one, never a mix. A failed write raises ConfigWriteError (`disk_full` →
HTTP 507) and leaves config.yaml byte-identical — except in the two
in-place cases ConfigWriteError describes.

Two layouts can't take a renamed-in file, and are written IN PLACE instead
(free space checked first, the old bytes put back if a write fails), the
way 3.4.147 wrote every save. The file is then read back; a write that
went through but doesn't read back as written is reported, NOT rolled
back:
  - config.yaml is a symlink into a folder this account can't write
    (updater.py supports that layout too);
  - config.yaml belongs to another account that lets this one write it (a
    group-shared manual install) — a rename would take it over.
A config.yaml this account can't write — another account's, or one made
read-only on purpose — is refused, with nothing changed.

`CONFIG_LOCK` serializes every in-process writer (web handlers run in the
threadpool; the collector's demo-port recovery runs on its own thread).
Hold it across the whole read-modify-write, not just the write. Lock order:
it may be taken while the maintenance lock (server._MAINT) is held — never
try for the maintenance lock while holding it.

Files kept beside config.yaml, in <install>/.backups/ (0600, in a 0700
directory):
  config.yaml.last-good      the config the service last STARTED with
                             (written by main.start once the web address
                             binds, and by the step that confirms an
                             update) — never by a save, so it is always a
                             config that worked; main.py falls back to it
                             when config.yaml can't be read or (Decision
                             2b) its web address can't be listened on
  config.yaml.broken.<ts>    a config.yaml that couldn't be read, kept as
                             it was when Aerodrome replaced it
  config-recovery.json       what the last startup recovery did, for the
                             Settings page (acknowledged by the user)

config.yaml itself is written 0600: it can hold API keys and notification
credentials.
"""
import errno
import os
import re
import threading
import time
from pathlib import Path

import yaml

import atomic_io

CONFIG_LOCK = threading.RLock()

# What main.load_config() insists on before it will start.
REQUIRED_SECTIONS = ("receiver", "web", "data", "logging", "retention")

CONFIG_MODE = atomic_io.PRIVATE_FILE_MODE
LAST_GOOD_NAME = "config.yaml.last-good"
BROKEN_PREFIX = "config.yaml.broken."
RECOVERY_NOTICE_NAME = "config-recovery.json"
# The update journal (updater.PENDING) and the restore marker (B4c's
# restore_io). While either is pending the running config isn't known to be
# good yet, so last-good is left alone; whatever confirms the update or
# finishes the restore refreshes it.
UPDATE_JOURNAL_NAME = ".update-pending.json"
RESTORE_MARKER_NAME = "pending-restore.json"

# The only automatic backups recovery may fall back to: the exact name
# Aerodrome writes (server.py's _CONFIG_AUTO_BACKUP_RE). Never a
# `.pre-restore` copy — a config the user deliberately replaced.
BAK_RE = re.compile(r"config\.yaml\.bak\.([0-9]{8}-[0-9]{6})")   # use .fullmatch


class ConfigWriteError(Exception):
    """A config.yaml write failed. `disk_full` → HTTP 507, else 500.

    config.yaml was NOT changed (the message ends "Nothing was changed."),
    except in two in-place cases (see _write_in_place), whose messages
    don't say that:
      - the write failed and the old bytes couldn't be put back either —
        the message says so;
      - the write went through but the file didn't read back as written.
        That is NOT rolled back: config.yaml is left as it read back. A
        disk that doesn't keep what it is given can't be trusted to keep
        the old bytes either."""

    def __init__(self, message, disk_full=False):
        super().__init__(message)
        self.disk_full = disk_full


def install_dir_of(path):
    """The directory config.yaml lives in (the install). Not resolved
    through a symlink: a config.yaml that links elsewhere still keeps its
    side files in the install's .backups/."""
    return Path(os.path.abspath(str(path))).parent


def backups_dir_of(path):
    return install_dir_of(path) / ".backups"


def last_good_path(path):
    return backups_dir_of(path) / LAST_GOOD_NAME


def recovery_notice_path(path):
    return backups_dir_of(path) / RECOVERY_NOTICE_NAME


def _target(path):
    # Write THROUGH a symlinked config.yaml rather than replacing the link.
    return Path(os.path.realpath(str(path)))


def _yaml_reason(e):
    """A short reason for a YAML error, with its position but none of the
    file's text (the message reaches the log and the web UI)."""
    problem = getattr(e, "problem", None) or getattr(e, "context", None)
    mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
    if problem and mark is not None:
        return f"not valid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}"
    if problem:
        return f"not valid YAML: {problem}"
    return "not valid YAML"


def check_loadable(text):
    """Parse `text` exactly as main.py does at startup (PyYAML safe_load)
    and apply its section check. Returns the dict; raises ValueError with a
    short reason."""
    if not text.strip():
        raise ValueError("the file is empty")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(_yaml_reason(e)) from None
    except RecursionError:
        raise ValueError("it is nested too deeply to be read as YAML") from None
    if not isinstance(data, dict):
        raise ValueError("it isn't a YAML mapping of settings")
    missing = [s for s in REQUIRED_SECTIONS if not isinstance(data.get(s), dict)]
    if missing:
        raise ValueError("missing section(s): " + ", ".join(missing))
    return data


def refuse_aliases(text):
    """Raise ValueError when `text` uses YAML anchors or aliases (&a / *a).
    Aerodrome never writes them; in a config that comes from outside (a
    restored backup, an imported file) a few hundred bytes of nested aliases
    expand to billions of nodes once the settings are compared or shown.
    Walks the parser's events — linear in the text, no tree is built. Only
    for incoming text: a hand-edited live config.yaml that uses them still
    starts (main.py never calls this)."""
    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                raise ValueError("it uses YAML anchors or aliases (& or *), which Aerodrome "
                                 "doesn't accept in a restored or imported config")
    except yaml.YAMLError as e:
        raise ValueError(_yaml_reason(e)) from None
    except RecursionError:
        raise ValueError("it is nested too deeply to be read as YAML") from None


def _as_write_error(e, name):
    if atomic_io.is_disk_full(e):
        return ConfigWriteError(f"Couldn't save {name}: the disk is full. Nothing was changed.",
                                disk_full=True)
    reason = os.strerror(e.errno) if getattr(e, "errno", None) else str(e)
    return ConfigWriteError(f"Couldn't save {name}: {reason}. Nothing was changed.")


def _account(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def _write_mode(path):
    """How `path` can be written: "atomic" (a temp file renamed over it) or
    "in_place" (see the module docstring). Raises ConfigWriteError for a
    file this account can't write. Root writes atomically (keeping the
    owner)."""
    name = Path(path).name
    target = _target(path)
    try:
        st = os.stat(target)
    except OSError:
        return "atomic"                     # a new file (or the write reports it)
    if os.geteuid() == 0:
        return "atomic"
    writable = os.access(target, os.W_OK)
    if st.st_uid != os.geteuid():
        if not writable:
            raise ConfigWriteError(
                f"Couldn't save {name}: it belongs to {_account(st.st_uid)} and Aerodrome, "
                f"running as {_account(os.geteuid())}, can't write to it. Nothing was changed.")
        return "in_place"
    if not writable:
        raise ConfigWriteError(
            f"Couldn't save {name}: it is read-only (mode {st.st_mode & 0o777:04o}). "
            f"Nothing was changed.")
    if os.path.islink(str(path)) and not os.access(target.parent, os.W_OK | os.X_OK):
        return "in_place"
    return "atomic"


class _InPlace:
    """A checked text waiting to be written over `target` in place."""

    def __init__(self, target, data):
        self.target, self.data = target, data


# Free space an in-place write insists on beyond the text itself.
IN_PLACE_MARGIN_BYTES = 1024 * 1024


def _stage_in_place(target, data, name):
    try:
        st = os.stat(target)
        if st.st_nlink != 1:
            # Writing in place would change every other name of the file too.
            raise ConfigWriteError(f"Couldn't save {name}: it has other hard links. "
                                   f"Nothing was changed.")
        vfs = os.statvfs(target)
        if vfs.f_bavail * vfs.f_frsize < len(data) + IN_PLACE_MARGIN_BYTES:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    except OSError as e:
        raise _as_write_error(e, name) from e
    return _InPlace(target, data)


def _write_in_place(staged, name):
    """Write over the file without replacing it (owner, group and — for
    another account's file — mode kept). If a write fails, the old bytes
    are put back. Once the write has gone through, the file is read back;
    a mismatch raises ConfigWriteError but is NOT rolled back (see
    ConfigWriteError)."""
    try:
        fd = os.open(str(staged.target), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:          # e.g. write-only for this account: nothing written
        raise _as_write_error(e, name) from e
    try:
        st = os.fstat(fd)
        old = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            old += chunk

        def put(data):
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        try:
            if st.st_uid == os.geteuid():
                os.fchmod(fd, CONFIG_MODE)
            put(staged.data)
        except OSError as e:
            try:
                put(old)
            except OSError:
                raise ConfigWriteError(
                    f"Couldn't save {name}: {os.strerror(e.errno) if e.errno else e}, and the "
                    f"old file couldn't be put back — Aerodrome puts the last settings that "
                    f"worked back at its next start.", disk_full=atomic_io.is_disk_full(e)) from e
            raise _as_write_error(e, name) from e
        os.lseek(fd, 0, os.SEEK_SET)
        got = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            got += chunk
        if got != staged.data:
            raise ConfigWriteError(f"Couldn't save {name}: it didn't read back as written.")
    finally:
        os.close(fd)


def stage_text(path, text, check=True):
    """Write `text` to a synced 0600 temp file beside `path` and return a
    handle for commit(). Every step that can fail for lack of space happens
    here, so a caller can stage before doing anything destructive and
    commit() after. (For an in-place layout the text is checked and free
    space confirmed here; commit() writes it.)
    check: refuse (ValueError) text the service couldn't start from."""
    if check:
        check_loadable(text)
    name = Path(path).name
    target = _target(path)
    data = text.encode("utf-8")
    if _write_mode(path) == "in_place":
        return _stage_in_place(target, data, name)
    try:
        return atomic_io.stage_bytes(target, data, mode=CONFIG_MODE, keep_mode=False,
                                     keep_owner=True)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            raise ConfigWriteError(
                f"Couldn't save {name}: Aerodrome can't create files in the folder "
                f"{target.parent} (it saves {name} by writing a new copy beside it and "
                f"renaming it). Nothing was changed.") from e
        raise _as_write_error(e, name) from e


def commit(tmp, path):
    """Put a staged file in place of `path` (a rename: needs no free space)."""
    if isinstance(tmp, _InPlace):
        _write_in_place(tmp, Path(path).name)
        return
    target = _target(path)
    try:
        atomic_io.commit(tmp, target)
    except OSError as e:
        atomic_io.discard(tmp)
        raise _as_write_error(e, Path(path).name) from e


def discard(tmp):
    if tmp and not isinstance(tmp, _InPlace):
        atomic_io.discard(tmp)


def write_text(path, text, check=True):
    """Replace `path` with `text`, all or nothing (0600)."""
    commit(stage_text(path, text, check=check), path)


def move_into_place(src, path):
    """Put the complete file `src` — a restore's staged settings, or the copy
    of config.yaml taken before a restore — in place of config.yaml
    (`path`), and remove `src`.

    A rename when it can be one (no free space needed, nothing copied):
    `src` must then be on the same filesystem as config.yaml's real file and
    that layout written atomically. Otherwise — another filesystem (a
    config.yaml symlinked elsewhere, a bind mount), an in-place layout — its
    text is written through write_text, then `src` is removed. Either way
    config.yaml is the old file or the new one, never a mix, and `src` is
    only removed once its text is in place: a repeat after a kill at any
    point finishes the job. Returns "renamed" or "written". Raises
    FileNotFoundError when `src` is gone, ConfigWriteError (config.yaml and
    `src` unchanged) when it can't be written. Unlike commit(), never
    deletes `src` on a failure: it may be the only copy."""
    src = Path(src)
    name = Path(path).name
    with CONFIG_LOCK:
        if not os.path.lexists(str(src)):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(src))
        if _write_mode(path) == "atomic":
            target = _target(path)
            try:
                os.replace(str(src), str(target))
            except OSError as e:
                if e.errno != errno.EXDEV:
                    if e.errno == errno.ENOENT and not os.path.lexists(str(src)):
                        raise
                    raise _as_write_error(e, name) from e
            else:
                atomic_io.fsync_dir(target.parent)
                return "renamed"
        text = src.read_text(encoding="utf-8")
        write_text(path, text, check=False)
        try:
            os.unlink(str(src))
        except FileNotFoundError:
            pass
        atomic_io.fsync_dir(src.parent)
        return "written"


def _render_preserving_comments(path, cfg):
    """Overlay `cfg` onto the comments and key order of the file on disk
    (ruamel). None when that isn't possible or wouldn't read back as cfg."""
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None
    import io
    try:
        ry = YAML()
        ry.preserve_quotes = True
        ry.indent(mapping=2, sequence=4, offset=2)
        with open(_target(path), encoding="utf-8") as f:
            doc = ry.load(f)
        if not isinstance(doc, dict):
            return None

        def _overlay(target, source):
            for k in list(target.keys()):
                if k not in source:
                    del target[k]
            for k, v in source.items():
                if k in target and isinstance(target[k], dict) and isinstance(v, dict):
                    _overlay(target[k], v)
                else:
                    target[k] = v

        _overlay(doc, cfg)
        buf = io.StringIO()
        ry.dump(doc, buf)
        text = buf.getvalue()
    except Exception:
        return None
    # ruamel writes YAML 1.2; main.py reads with PyYAML (1.1). A value
    # ruamel writes fresh (not already quoted in the file) can change type
    # on the way back — an unquoted 22:00 is the base-60 integer 1320 to
    # PyYAML. Only keep the comment-preserving render if it reads back equal.
    try:
        if yaml.safe_load(text) != cfg:
            return None
    except yaml.YAMLError:
        return None
    return text


def render_config(path, cfg):
    """The text to write for `cfg`: the file's comments kept when that reads
    back exactly as `cfg`, else a plain dump. Raises ValueError when the
    result couldn't start the service."""
    text = _render_preserving_comments(path, cfg)
    if text is None:
        text = yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False,
                              allow_unicode=True)
    check_loadable(text)
    return text


def save_config(path, cfg):
    """Persist `cfg` to `path` atomically, keeping the file's comments when
    possible. Raises ConfigWriteError (file untouched) or ValueError (cfg
    couldn't start the service). Returns the text written. last-good is NOT
    refreshed: a saved config hasn't started yet (a web port another
    program holds only fails at the next start, and Decision 2b must then
    still have the address that last worked)."""
    with CONFIG_LOCK:
        text = render_config(path, cfg)
        write_text(path, text, check=False)
    return text


def pending(path):
    """True while an update or a restore isn't confirmed yet. Never raises:
    a marker this account can't see counts as not pending (os.path.exists —
    Path.exists() raises PermissionError there on Python 3.10–3.12)."""
    inst = install_dir_of(path)
    return (os.path.exists(inst / UPDATE_JOURNAL_NAME)
            or os.path.exists(inst / ".backups" / RESTORE_MARKER_NAME))


def write_last_good(path, text=None):
    """Record `text` (default: config.yaml as it is now) as the last config
    that worked — called once a config has STARTED (main.start after the
    bind check, the update-confirm step). Best effort — never fails the
    start it follows — and skipped while an update or restore is pending.
    True when written."""
    try:
        if pending(path):
            return False
        if text is None:
            text = _target(path).read_text(encoding="utf-8")
        check_loadable(text)
        atomic_io.ensure_private_dir(backups_dir_of(path))
        atomic_io.write_atomic(last_good_path(path), text, mode=CONFIG_MODE, keep_mode=False)
        return True
    except Exception:
        return False


def read_last_good(path):
    """(cfg, text) of last-good, or (None, None) when missing or unreadable."""
    try:
        text = last_good_path(path).read_text(encoding="utf-8")
        return check_loadable(text), text
    except (OSError, ValueError):
        return None, None


def _db_exists(cfg, inst):
    db = (cfg.get("data") or {}).get("db_file")
    if not db or not isinstance(db, str):
        return False
    p = Path(db)
    return (p if p.is_absolute() else inst / p).exists()


def _candidates(path, last_good_by_date=False):
    """(name, text, cfg, when) of every copy recovery may use, best first:
    last-good, then the automatic config.yaml.bak.<ts> backups newest first
    (by the timestamp in the name — a copy keeps its source's mtime). A
    backup whose database file is gone is skipped: it would start Aerodrome
    on an empty or archived database.

    last_good_by_date: last-good may be older than the backups (it was kept
    by a newer release, then an older one that doesn't refresh it ran) — it
    then takes its place among them by date instead of coming first."""
    inst = install_dir_of(path)
    lg = last_good_path(path)
    out = []
    cfg, text = read_last_good(path)
    if cfg is not None:
        try:
            when = lg.stat().st_mtime
        except OSError:
            when = None
        out.append((f".backups/{LAST_GOOD_NAME}", text, cfg, when))
    try:
        baks = sorted((p for p in inst.iterdir() if BAK_RE.fullmatch(p.name) and p.is_file()),
                      key=lambda p: p.name, reverse=True)
    except OSError:
        baks = []
    for p in baks:
        try:
            text = p.read_text(encoding="utf-8")
            cfg = check_loadable(text)
        except (OSError, ValueError):
            continue
        if not _db_exists(cfg, inst):
            continue
        try:
            when = time.mktime(time.strptime(BAK_RE.fullmatch(p.name).group(1),
                                             "%Y%m%d-%H%M%S"))
        except (ValueError, OverflowError):
            when = None
        out.append((p.name, text, cfg, when))
    if last_good_by_date:
        out.sort(key=lambda c: c[3] if c[3] is not None else float("-inf"), reverse=True)
    return out


def _keep_broken_copy(path):
    """Keep config.yaml as it is now in .backups/config.yaml.broken.<ts>
    (a hard link — needs no space on a full disk — made 0600, else a 0600
    copy). Returns its name, or None when no copy could be made — then
    config.yaml must not be replaced. Never a hard link when config.yaml is
    written in place: the write would change the kept copy too."""
    target = _target(path)
    try:
        bdir = atomic_io.ensure_private_dir(backups_dir_of(path))
    except OSError:
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{BROKEN_PREFIX}{ts}"
    n = 1
    while os.path.lexists(dest):
        n += 1
        dest = bdir / f"{BROKEN_PREFIX}{ts}-{n}"
    try:
        linkable = _write_mode(path) == "atomic"
    except ConfigWriteError:
        linkable = False
    if linkable:
        try:
            os.link(target, dest)
            try:
                # The same inode as config.yaml, which is replaced next.
                os.chmod(dest, CONFIG_MODE)
            except OSError:
                pass
            return dest.name
        except OSError:
            pass
    try:
        atomic_io.write_atomic(dest, target.read_bytes(), mode=CONFIG_MODE, keep_mode=False)
        return dest.name
    except OSError:
        return None


def _web(cfg):
    w = cfg.get("web") or {}
    return w.get("host"), w.get("port")


def _record_notice(path, info, log):
    try:
        atomic_io.ensure_private_dir(backups_dir_of(path))
        atomic_io.write_json_atomic(recovery_notice_path(path), info, mode=CONFIG_MODE)
    except OSError as e:
        log(f"WARNING: could not record the config recovery for the Settings page: {e}")


def recover(path, reason, log=print, kind="unreadable", detail=None, sources=None,
            last_good_by_date=False):
    """config.yaml can't be used. Put the best copy that can (see
    _candidates; `sources` limits them by name) in its place, keeping the
    unusable file as .backups/config.yaml.broken.<ts>, and record a notice
    for the Settings page. config.yaml is only replaced once that copy
    exists, and the WARNING and the notice come BEFORE it is replaced — a
    kill right after the replace can't make the recovery silent. Returns
    (cfg, info), or (None, None) when no copy can be used.

    info: {source, source_date, web_host, web_port, broken_copy, rewritten}.
    Callers make sure no other Aerodrome owns the install first."""
    for name, text, cfg, when in _candidates(path, last_good_by_date=last_good_by_date):
        if sources is not None and name not in sources:
            continue
        host, port = _web(cfg)
        broken = _keep_broken_copy(path)
        when_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "unknown date"
        if kind == "bind":
            log(f"WARNING: Aerodrome couldn't listen on {detail or 'the address in config.yaml'} "
                f"from {Path(path).name} ({reason}), so it is starting on the last settings that "
                f"worked ({name}, {when_txt}; web {host}:{port})."
                + (f" Your file was kept as .backups/{broken}." if broken else ""))
        else:
            log(f"WARNING: {Path(path).name} couldn't be read ({reason}). Aerodrome is running "
                f"on the last settings that worked ({name}, {when_txt}; web port {port})."
                + (f" Your file was kept as .backups/{broken}." if broken else ""))
        info = {"at": time.time(), "kind": kind, "reason": str(reason), "source": name,
                "source_date": when, "web_host": host, "web_port": port,
                "broken_copy": f".backups/{broken}" if broken else None,
                "rewritten": broken is not None, "acknowledged": False}
        if detail:
            info["tried"] = detail
        _record_notice(path, info, log)
        if broken is None:
            log(f"WARNING: could not keep a copy of {Path(path).name}, so it was left as it "
                f"is; Aerodrome is running on {name} until it is fixed.")
            return cfg, info
        try:
            with CONFIG_LOCK:
                write_text(path, text, check=False)
        except ConfigWriteError as e:
            log(f"WARNING: could not put {name} in place of {Path(path).name}: {e}")
            info["rewritten"] = False
            _record_notice(path, info, log)
        return cfg, info
    return None, None


def read_recovery_notice(path):
    return atomic_io.read_json(recovery_notice_path(path))


def ack_recovery_notice(path):
    """Mark the notice seen. True when there was one. Raises OSError."""
    p = recovery_notice_path(path)
    info = atomic_io.read_json(p)
    if info is None:
        return False
    info["acknowledged"] = True
    atomic_io.write_json_atomic(p, info, mode=CONFIG_MODE)
    return True
