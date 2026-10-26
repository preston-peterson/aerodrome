"""Aerodrome's in-app update: staged apply, boot-time guard, automatic rollback.

STDLIB ONLY, and imports NO other Aerodrome module, on purpose: main.py runs
the guard (on_boot) before any third-party or app import, so this file must
load and work from any half-swapped tree, whatever state the rest of the
install is in. Its small JSON/fsync helpers are private copies for the same
reason (atomic_io.py is not imported here).

Apply (driven by server.py's update endpoints, one at a time):
  check_versions       refuse a downgrade / the same version unless asked
  preflight_space      enough free space to stage the release
  preflight_permissions  every live directory the swap moves is writable
  stage                copy the release into <install>/.update-staging-<ts>/
  deps_changed         requirement lines differ → install_deps (pip) runs
  verify               a fresh interpreter compiles the top-level modules
                       and imports main + server from the staging dir
  swap_in              .update-old-<ts>/ and .update-failed-<ts>/ are made
                       and the journal written first (while there is space),
                       then per entry: a file is hard-linked (or durably
                       copied) into .update-old-<ts>/ and the staged copy
                       replaces it (never absent); a directory takes two
                       renames. Only the names the caller staged are swapped.
  (caller restarts the service; if that fails it calls swap_back itself)

Boot (main.py, before any other import), on_boot():
  'committing'          → finish the commit
  'swapping' / 'rolling_back' → idempotent rollback, then re-exec
  'pending_first_boot'  → a rollback that was cut short (it left its marks:
                          the old dir lost an entry, or the tree isn't whole
                          and something is parked in the failed dir): finish
                          it; the journal is for a version other than the
                          one installed (an older release without the guard
                          was applied over it): set it aside; first good
                          answer over 10 min ago: commit; 2 starts already
                          counted: roll back; otherwise count this start (if
                          that write fails: roll back now — counting is
                          impossible, so the guard could otherwise never
                          fire). A rollback the guard decides on is skipped
                          when the previous release's copy is gone (nothing
                          to put back).
The running new version confirms itself (main.py's confirm thread):
note_served() on the first good /api/version answer, commit() 30 s later.
A restart the new version's own UI asks for after it has served commits
first (server.py), so a planned restart is never counted as a failed start.

N-1 rule: the journal's and the result file's field sets are additive-only,
and the state set {swapping, pending_first_boot, rolling_back, committing}
is frozen. A newer updater must read every older journal (test_update_apply
keeps frozen copies of this version's journals); an unknown state is left
alone, and so is a journal whose paths aren't the shapes written here.
"""
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PENDING = ".update-pending.json"
LAST_RESULT = ".update-last-result.json"
STAGING_PREFIX = ".update-staging-"
OLD_PREFIX = ".update-old-"
FAILED_PREFIX = ".update-failed-"
STALE_PREFIX = ".update-pending.stale-"
CONFIG_SNAPSHOT = ".config.yaml.pre-update"
JOURNAL_VERSION = 1
STATES = frozenset({"swapping", "pending_first_boot", "rolling_back", "committing"})

MAX_FAILED_STARTS = 2         # starts counted before the next one rolls back
HEALTHY_AFTER_SEC = 30        # serving this long after the first good answer → commit
COMMIT_AFTER_SEC = 600        # a first good answer this old at a start → commit
SERVE_DEADLINE_SEC = 300      # no good answer within this → see main.py
KEEP_FAILED = 2               # non-empty .update-failed-<ts>/ dirs kept
PIP_TIMEOUT_SEC = 600
VERIFY_TIMEOUT_SEC = 180
STAGE_HEADROOM_BYTES = 64 * 1024 * 1024
VERSION_READ_LIMIT = 64       # bytes of a VERSION file ever read

# Never part of a release's entry set, whatever the staged dir contains.
_NEVER_SWAP = {"update", "__pycache__", ".git", PENDING, LAST_RESULT}
# The install's own data: never swapped or moved by an update, whatever a
# caller, a staged directory or a journal says.
_PROTECTED = {"config.yaml", "venv", ".backups", "logs", ".tracker.pid"}
_PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm")
# A journal's ts: what server.py's strftime writes (and test names), never
# a path ('/' and '.' excluded).
_TS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The guard's markers in a release's main.py (see main.py's guard block).
_GUARD_MARKERS = ("import updater", "on_boot(")

_COMMIT_LOCK = threading.Lock()   # the confirm thread vs a planned restart


class ApplyError(Exception):
    """An update refusal or failure that left the live install as it was."""

    def __init__(self, code, message, http_status=500, detail=""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail


# ---------- small private I/O helpers (no app imports, on purpose) ----------

def _fsync_dir(d):
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _fsync_file(p):
    try:
        fd = os.open(str(p), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_json(path, obj):
    """Temp file + fsync + replace + directory fsync. Raises OSError."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def read_json(path):
    """A JSON object from `path`, or None when missing or unparseable."""
    try:
        with open(path) as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def pending(install_dir):
    """The update journal (dict), or None when there is none (or it can't be read)."""
    return read_json(Path(install_dir) / PENDING)


def last_result(install_dir):
    return read_json(Path(install_dir) / LAST_RESULT)


def read_version(path, limit=VERSION_READ_LIMIT):
    """A VERSION file's text (stripped), reading at most `limit` bytes; None
    when it can't be read. A real version is a dozen bytes; a staged VERSION
    comes from a zip and could be anything."""
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return None
    return data.decode("utf-8", "replace").strip()


def _lexists(p):
    return os.path.lexists(p)


def _is_dir(p):
    return os.path.isdir(p) and not os.path.islink(p)


def _remove(p):
    if _is_dir(p):
        shutil.rmtree(p)
    else:
        os.unlink(p)


def _rmdir_quiet(p):
    """Remove `p` when it is an empty directory; anything else is kept."""
    try:
        os.rmdir(p)
    except OSError:
        pass


def _same_file(a, b):
    try:
        return os.path.samestat(os.lstat(a), os.lstat(b))
    except OSError:
        return False


def _copy_durable(src, dst, follow_symlinks=False):
    """Copy src to dst so that dst only ever exists COMPLETE: a temp name
    in dst's directory, fsync, then rename. An interrupted copy (disk full,
    I/O error, a kill or a power cut) leaves no dst — a rollback that later
    finds dst would otherwise install a truncated file. A symlink is copied
    as a symlink unless `follow_symlinks`."""
    dst = Path(dst)
    tmp = dst.with_name(f".{dst.name}.copying")
    try:
        if _lexists(tmp):
            os.unlink(tmp)
        shutil.copy2(src, tmp, follow_symlinks=follow_symlinks)
        if not os.path.islink(tmp):
            _fsync_file(tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(dst.parent)


def _link_or_copy(src, dst):
    """Hard link src at dst; copy (durably) when the filesystem or policy
    can't link (fs.protected_hardlinks on files another account owns,
    filesystems without hard links)."""
    try:
        os.link(src, dst, follow_symlinks=False)
    except OSError as e:
        if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EMLINK, errno.EXDEV,
                           getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            raise
        _copy_durable(src, dst)


def _ensure_dir(p):
    if not _is_dir(p):
        os.mkdir(p)


def schema_version_ro(db_file):
    """MAX(schema_version.version) read in read-only mode, or None."""
    if not db_file:
        return None
    try:
        import sqlite3
        from urllib.parse import quote
        conn = sqlite3.connect(f"file:{quote(str(db_file))}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - informational only
        return None


def _swappable(name):
    """A bare top-level name an update may swap: never a path, never the
    install's own data, never update machinery."""
    return (isinstance(name, str) and name not in ("", ".", "..")
            and "/" not in name and "\0" not in name
            and name not in _NEVER_SWAP and name not in _PROTECTED
            and not name.endswith(_PROTECTED_SUFFIXES)
            and not name.startswith(".update-"))


def _journal_problem(j):
    """None when the journal's path fields have the shapes swap_in writes;
    else what is off. The guard renames and removes what these name, so a
    damaged or hand-edited journal must never point it outside the install
    or at the install's own data."""
    ts = j.get("ts")
    if not isinstance(ts, str) or not _TS_RE.match(ts):
        return "ts"
    if j.get("staging_dir") != STAGING_PREFIX + ts or j.get("old_dir") != OLD_PREFIX + ts:
        return "staging_dir/old_dir"
    entries = j.get("entries")
    if not isinstance(entries, list) or not all(_swappable(n) for n in entries):
        return "entries"
    had_old = j.get("had_old", entries)
    if not isinstance(had_old, list) or not all(_swappable(n) for n in had_old):
        return "had_old"
    return None


def _swap_intact(install_dir, j):
    """True while the tree is exactly what a completed swap_in leaves: every
    swapped entry live, and every replaced one kept in the old dir."""
    old_dir = install_dir / j["old_dir"]
    if not all(_lexists(install_dir / n) for n in j["entries"]):
        return False
    return all(_lexists(old_dir / n) for n in j.get("had_old", ()))


def _parked_any(install_dir, j):
    """True when something is parked in .update-failed-<ts>/ (swap_in
    makes it empty; only a rollback puts anything there)."""
    try:
        with os.scandir(install_dir / f"{FAILED_PREFIX}{j['ts']}") as it:
            return any(True for _ in it)
    except OSError:
        return False


def _rollback_cut_short(install_dir, j):
    """True when a rollback started changing the tree and was stopped. Its
    'rolling_back' write is best effort (it fails on a full disk), so this
    reads the marks every rollback leaves before or with its first change,
    and that need no free space:
      - .update-old-<ts>/ is there but has lost an entry it was given (a
        rollback moves them back one by one), or
      - the swapped tree isn't whole and something is parked in
        .update-failed-<ts>/ (a rollback parks there before it moves a
        live directory or takes an added entry away).
    A tree that is merely not whole is NOT one: an old dir removed by hand
    (to free space, say) leaves a whole new release with nothing to put
    back, and 'finishing a rollback' would only take its added files away."""
    old_dir = install_dir / j["old_dir"]
    if _is_dir(old_dir) and not all(_lexists(old_dir / n) for n in j.get("had_old", ())):
        return True
    return not _swap_intact(install_dir, j) and _parked_any(install_dir, j)


def _disk_full(e):
    return getattr(e, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))


# ---------- versions ----------

def parse_version(v):
    """'3.4.147' or 'v3.4.147' → (3, 4, 147); None for anything else
    (pre-release / build suffixes, non-numeric parts, not three parts)."""
    if v is None:
        return None
    s = str(v).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    if not s or "-" in s or "+" in s:
        return None
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def shown_version(v, limit=40):
    """A staged version string safe to put in a message or a log: only
    letters, digits and . _ - + space, at most `limit` characters."""
    s = re.sub(r"[^A-Za-z0-9._ +-]", "?", str(v))
    return s if len(s) <= limit else s[:limit] + "... (truncated)"


def check_versions(current, staged, allow_downgrade=False, allow_reinstall=False):
    new = parse_version(staged)
    if new is None:
        raise ApplyError("bad_version",
                         f"The staged VERSION ({shown_version(staged)}) isn't a release "
                         "number (X.Y.Z). Nothing was changed.", 400)
    cur = parse_version(current)
    if cur is None:          # unknown/dev install: nothing to compare against
        return
    if new < cur and not allow_downgrade:
        raise ApplyError("downgrade_refused",
                         f"The staged release {staged} is older than the running {current}. "
                         "Nothing was changed.", 409)
    if new == cur and not allow_reinstall:
        raise ApplyError("same_version",
                         f"The staged release {staged} is the version already running. "
                         "Nothing was changed.", 409)


def has_guard(release_root):
    """True when a release's main.py runs this update guard (3.4.147 and
    later). A release without it can never confirm or roll back an update."""
    root = Path(release_root)
    try:
        text = (root / "main.py").read_text(errors="replace")
    except OSError:
        return False
    return (root / "updater.py").is_file() and all(m in text for m in _GUARD_MARKERS)


# ---------- apply side ----------

def release_entries(update_root, preserve):
    """Top-level names of a staged release that an apply swaps in."""
    return sorted(p.name for p in Path(update_root).iterdir()
                  if p.name not in preserve and _swappable(p.name)
                  and not p.name.startswith((STAGING_PREFIX, OLD_PREFIX, FAILED_PREFIX)))


def tree_bytes(root, names):
    total = 0
    for n in names:
        p = Path(root) / n
        if p.is_symlink():
            continue
        if p.is_dir():
            total += sum(f.stat().st_size for f in p.rglob("*")
                         if f.is_file() and not f.is_symlink())
        elif p.is_file():
            total += p.stat().st_size
    return total


def preflight_space(install_dir, update_root, preserve, headroom=STAGE_HEADROOM_BYTES):
    need = tree_bytes(update_root, release_entries(update_root, preserve)) + headroom
    free = shutil.disk_usage(str(install_dir)).free
    if free < need:
        raise ApplyError("no_space",
                         f"Not enough free disk space to stage the update: need about "
                         f"{need // (1024 * 1024)} MB, {free // (1024 * 1024)} MB available. "
                         "Nothing was changed.", 507)


def permission_problems(install_dir, entries):
    """Paths an apply would need to write but can't, as absolute strings.

    The install directory itself must be writable (staging, journal, the
    file swap). A live top-level DIRECTORY the swap moves must be writable
    too: renaming a directory to a different parent rewrites its '..'
    entry, which needs write permission on the directory itself (EACCES
    otherwise, even when both parents are writable)."""
    install_dir = Path(install_dir)
    bad = []
    if not os.access(str(install_dir), os.W_OK | os.X_OK):
        bad.append(str(install_dir))
    for name in entries:
        live = install_dir / name
        if _is_dir(live) and not os.access(str(live), os.W_OK | os.X_OK):
            bad.append(str(live))
    return bad


def fix_command(paths):
    """The command that makes `paths` writable by this account, whoever owns
    them now and whatever their mode: chown alone leaves a read-only folder
    read-only, chmod alone can't fix one another account owns."""
    import shlex
    try:
        import pwd
        user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 - no passwd entry: show the numeric id
        user = str(os.geteuid())
    q = " ".join(shlex.quote(p) for p in paths)
    return f"sudo chown -R {shlex.quote(user)}: {q} && sudo chmod -R u+w {q}"


def permission_message(paths):
    return (f"{', '.join(paths)} {'is' if len(paths) == 1 else 'are'}n't writable by the "
            f"account Aerodrome runs as. Fix with: {fix_command(paths)}")


def preflight_permissions(install_dir, entries):
    bad = permission_problems(install_dir, entries)
    if bad:
        raise ApplyError("not_writable",
                         "Updates can't be applied yet: " + permission_message(bad)
                         + " Nothing was changed.", 409)


def _sweep_staging(install_dir, keep=()):
    for p in Path(install_dir).glob(STAGING_PREFIX + "*"):
        if p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def stage(install_dir, update_root, preserve, ts):
    """Copy the release into .update-staging-<ts>/ (same filesystem as the
    install, so the swap is renames). update/ and the live tree are untouched."""
    install_dir = Path(install_dir)
    j = pending(install_dir)
    _sweep_staging(install_dir, keep={j.get("staging_dir")} if j else ())
    staging = install_dir / f"{STAGING_PREFIX}{ts}"
    try:
        staging.mkdir()
        for name in release_entries(update_root, preserve):
            src, dst = Path(update_root) / name, staging / name
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, dst, symlinks=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        # Zip extraction drops the execute bit; scripts are run with
        # `sudo bash <script>` anyway, but keep them executable as shipped.
        for sh in staging.rglob("*.sh"):
            if sh.is_file() and not sh.is_symlink():
                sh.chmod(sh.stat().st_mode | 0o111)
        # The two boot-critical files must survive a power cut right after
        # the swap: the guard in main.py (via updater.py) is what undoes it.
        for name in ("main.py", "updater.py"):
            if (staging / name).is_file():
                _fsync_file(staging / name)
        _fsync_dir(staging)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise ApplyError("no_space" if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))
                         else "stage_failed",
                         f"Could not stage the update: {e}. Nothing was changed.",
                         507 if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) else 500)
    return staging


def _requirement_lines(path):
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    return sorted(ln.strip() for ln in text.splitlines()
                  if ln.strip() and not ln.strip().startswith("#"))


def deps_changed(staging, install_dir):
    """True when the staged release's requirement lines differ from the live
    ones (blank lines and comments ignored — line 1 is the `# Version:`
    header, so the files themselves never match)."""
    new = _requirement_lines(Path(staging) / "requirements.txt")
    if new is None:
        return False
    return new != _requirement_lines(Path(install_dir) / "requirements.txt")


def install_deps(staging, pip, timeout=PIP_TIMEOUT_SEC):
    """pip install -r <staging>/requirements.txt into the live venv.
    Returns (ok, detail). A failure does NOT refuse the update on its own:
    verify() decides whether the new version still loads. pip never waits
    for input (an index that asks for credentials fails at once instead of
    holding the update for the whole timeout)."""
    req = Path(staging) / "requirements.txt"
    if not pip or not Path(pip).exists() or not req.exists():
        return True, ""
    try:
        r = subprocess.run([str(pip), "install", "-r", str(req), "-q",
                            "--disable-pip-version-check", "--no-input"],
                           cwd=str(staging), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"pip did not finish within {timeout} s"
    except OSError as e:
        return False, f"could not run pip: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or f"pip exited with status {r.returncode}").strip()[-4000:]
    return True, ""


# The test-load. argv[1] is the release path, so the release's main.py
# update guard (which only acts for `start`) does nothing. -I drops the
# script directory from sys.path on 3.11+, so the release dir is inserted here.
_PROBE = r"""
import os, sys
root = sys.argv[1]
bad = []
for fn in sorted(os.listdir(root)):
    if fn.endswith(".py"):
        p = os.path.join(root, fn)
        try:
            with open(p, "rb") as f:
                compile(f.read(), p, "exec", dont_inherit=True)
        except Exception as e:
            bad.append("%s: %s: %s" % (fn, type(e).__name__, e))
if bad:
    sys.stderr.write("\n".join(bad[:20]) + "\n")
    sys.exit(3)
sys.path.insert(0, root)
os.chdir(root)
import main
assert callable(getattr(main, "main", None)) and callable(getattr(main, "start", None)), \
    "main.py lost main() or start()"
import server
assert callable(getattr(server, "get_app", None)), "server.get_app is missing"
print("probe-ok")
"""


def verify(staging, python, timeout=VERIFY_TIMEOUT_SEC):
    """Compile the release's top-level modules and import main + server in a
    fresh interpreter (the live venv's). scripts/ and tools/ are not checked:
    they don't stop the service from starting. HOME and TMPDIR point at a
    throwaway directory OUTSIDE the release, so nothing a library writes
    there can become part of what is swapped in."""
    import tempfile
    home = tempfile.mkdtemp(prefix="aerodrome-verify-")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "TMPDIR": home,
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    try:
        r = subprocess.run([str(python), "-I", "-B", "-W", "ignore::SyntaxWarning",
                            "-c", _PROBE, str(staging)],
                           cwd=str(staging), capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ApplyError("verify_failed", f"The new release didn't finish loading within "
                         f"{timeout} s. Aerodrome's files were not changed.",
                         detail=f"test-load timed out after {timeout} s")
    except OSError as e:
        raise ApplyError("verify_failed", f"Could not test-load the new release: {e}. "
                         "Aerodrome's files were not changed.", detail=str(e))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if r.returncode != 0 or "probe-ok" not in r.stdout:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-15:]
        raise ApplyError("verify_failed", "The new release failed to load (a syntax error "
                         "or a missing package). Aerodrome's files were not changed.",
                         detail="\n".join(tail))


# ---------- swap ----------

def swap_in(install_dir, staging, ts, from_version, to_version, extra=None, entries=None):
    """Switch the staged release in. The journal is written (durably) before
    the first change, so a kill at any point is undone by on_boot().

    `entries`: the names stage() copied (server.py passes them). Anything
    else found in the staging dir — something the test-load or pip wrote
    there — refuses the swap: it would replace the live copy. Without
    `entries`, every swappable name in the staging dir is the release."""
    install_dir, staging = Path(install_dir), Path(staging)
    if not isinstance(ts, str) or not _TS_RE.match(ts):
        raise ApplyError("swap_failed", f"Bad update id {ts!r}. Nothing was changed.")
    try:
        present = sorted(p.name for p in staging.iterdir())
    except OSError as e:
        raise ApplyError("swap_failed", f"Could not read the staged release: {e}. "
                         "Nothing was changed.")
    if entries is None:
        entries = [n for n in present if _swappable(n)]
    entries = sorted(entries)
    unexpected = sorted((set(present) - set(entries))
                        | {n for n in entries if not _swappable(n)})
    missing = sorted(set(entries) - set(present))
    if unexpected or missing:
        raise ApplyError(
            "stage_failed",
            "The staged copy of the new release changed while it was being checked "
            "(something wrote files into it while it loaded). Nothing was changed.", 500,
            detail=f"unexpected: {unexpected[:20]} missing: {missing[:20]}")
    old_dir = install_dir / f"{OLD_PREFIX}{ts}"
    failed_dir = install_dir / f"{FAILED_PREFIX}{ts}"
    made = []
    try:
        old_dir.mkdir()
        made.append(old_dir)
        # Made now, while there is space: a rollback on a full disk can't
        # create a directory, but it can rename into one that exists.
        failed_dir.mkdir()
        made.append(failed_dir)
        had_old = [n for n in entries if _lexists(install_dir / n)]
        cfg = install_dir / "config.yaml"
        if cfg.is_file():          # its content, even when it is a symlink
            _copy_durable(cfg, old_dir / CONFIG_SNAPSHOT, follow_symlinks=True)
        journal = {"journal_version": JOURNAL_VERSION, "state": "swapping",
                   "from_version": from_version, "to_version": to_version, "ts": ts,
                   "entries": entries, "had_old": had_old, "staging_dir": staging.name,
                   "old_dir": old_dir.name, "failed_starts": 0, "applier_pid": os.getpid(),
                   "started_at": time.time()}
        journal.update(extra or {})
        _write_json(install_dir / PENDING, journal)
    except OSError as e:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)
        full = e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))
        raise ApplyError("no_space" if full else "swap_failed",
                         f"Could not start the update: {e}. Nothing was changed.",
                         507 if full else 500)
    try:
        for name in entries:
            live, new, old = install_dir / name, staging / name, old_dir / name
            if _lexists(live):
                if _is_dir(live) or _is_dir(new):
                    os.rename(live, old)
                    os.rename(new, live)
                else:
                    _link_or_copy(live, old)
                    os.replace(new, live)
            else:
                os.rename(new, live)
        _fsync_dir(install_dir)
        try:
            staging.rmdir()               # empty now; before the state flips (a
        except OSError:                   # kill here still rolls back cleanly)
            pass
        journal["state"] = "pending_first_boot"
        _write_json(install_dir / PENDING, journal)
    except OSError as e:
        full = e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))
        try:
            swap_back(install_dir, journal, reason=f"swap failed: {e}",
                      restore_config=False, record=False, discard_failed=True)
        except OSError as e2:
            # The journal (still present) makes the next start finish this.
            raise ApplyError("swap_failed", f"Could not switch the new files in ({e}), and "
                             f"putting the previous files back stopped too ({e2}). "
                             "Aerodrome finishes putting them back at its next start.",
                             detail=f"{e}\n{e2}")
        raise ApplyError("no_space" if full else "swap_failed",
                         f"Could not switch the new files in: {e}. "
                         "The previous version was put back.", 507 if full else 500)
    return journal


def _restore_config(snap, cfg):
    """Put the pre-update config.yaml back. A config.yaml that is a symlink
    (to a file kept elsewhere) is written THROUGH, so the link survives and
    the real file gets the old content back."""
    cfg = Path(cfg)
    if not os.path.islink(cfg):
        os.replace(snap, cfg)
        return
    tgt = Path(os.path.realpath(cfg))
    try:
        _copy_durable(snap, tgt)
    except OSError:
        # The target's directory isn't writable by this account (or the
        # copy can't land there): rewrite the file in place. The snapshot
        # is kept until old_dir goes, so a replay copies it again.
        shutil.copyfile(snap, tgt)


def swap_back(install_dir, journal, reason, restore_config=True, record=True,
              discard_failed=False):
    """Undo a (possibly partial) swap. Safe to run any number of times, and
    to re-run after a kill at any point inside it. `discard_failed`: drop
    the parked copies of the new release (in-process undo: the release is
    still staged in update/)."""
    install_dir = Path(install_dir)
    problem = _journal_problem(journal)
    if problem:
        raise OSError(errno.EINVAL, f"the update journal has an unexpected {problem}")
    if journal.get("state") != "rolling_back":
        journal = dict(journal, state="rolling_back", rollback_reason=reason)
        if not restore_config:
            # An undo in this process (the swap or the restart failed): the
            # new version never ran, so config.yaml is the user's own. If
            # the undo stops part-way, the old process keeps running, and
            # the start that finishes the undo (maybe days later) must not
            # put the pre-update copy back over settings saved since.
            journal["restore_config"] = False
        try:
            _write_json(install_dir / PENDING, journal)   # best effort: replays are idempotent
        except OSError:
            pass
    staging = install_dir / journal["staging_dir"]
    old_dir = install_dir / journal["old_dir"]
    failed = install_dir / f"{FAILED_PREFIX}{journal['ts']}"
    had_old = set(journal.get("had_old", journal["entries"]))
    unkept = []
    for name in reversed(journal["entries"]):
        live, old, new = install_dir / name, old_dir / name, staging / name
        if _lexists(old):
            if _lexists(live):
                if not _is_dir(live) and not _is_dir(old) and _same_file(live, old):
                    os.unlink(old)        # linked but never replaced: live is the old file
                    continue
                _ensure_dir(failed)
                dst = failed / name
                if _lexists(dst):
                    _remove(dst)
                if _is_dir(live) or _is_dir(old):
                    os.rename(live, dst)
                    os.rename(old, live)
                else:
                    try:
                        _link_or_copy(live, dst)
                    except OSError as e:
                        if not _disk_full(e):
                            raise
                        # No hard links on this filesystem and no room for a
                        # copy: the new release's copy of this file is only
                        # kept for inspection, so let it go. The live file
                        # is still replaced in one step (never missing).
                        unkept.append(name)
                    os.replace(old, live)
            else:
                os.rename(old, live)
        elif name not in had_old and _lexists(live) and not _lexists(new):
            _ensure_dir(failed)           # an entry the release added
            dst = failed / name
            if _lexists(dst):
                _remove(dst)
            os.rename(live, dst)
        # else: already restored by an earlier attempt, or never swapped
    if unkept:
        _log(f"the disk is full: {len(unkept)} file(s) of the release being taken out "
             f"were not kept in {failed.name}/ ({', '.join(unkept[:5])}"
             f"{', ...' if len(unkept) > 5 else ''})")
    snap = old_dir / CONFIG_SNAPSHOT
    if restore_config and _lexists(snap):
        _restore_config(snap, install_dir / "config.yaml")
    _fsync_dir(install_dir)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old_dir, ignore_errors=True)
    if discard_failed:
        shutil.rmtree(failed, ignore_errors=True)
    else:
        _rmdir_quiet(failed)              # nothing parked: nothing to keep
    if record:
        try:
            _write_json(install_dir / LAST_RESULT, {
                "result": "rolled_back", "from_version": journal.get("from_version"),
                "to_version": journal.get("to_version"), "reason": reason,
                "failed_dir": failed.name if _is_dir(failed) else None,
                "schema_before": journal.get("schema_before"),
                "schema_after": schema_version_ro(journal.get("db_file")),
                "failed_starts": journal.get("failed_starts"),
                "previous_loads": journal.get("previous_loads"),
                "at": time.time(), "acknowledged": False})
        except OSError as e:
            _log(f"could not record the rollback result: {e}")
    try:
        (install_dir / PENDING).unlink()
    except FileNotFoundError:
        pass


def _old_to_backups(install_dir, old_name, ts):
    """Move .update-old-<ts>/ (the previous release) into .backups/<ts>/,
    dropping its config.yaml copy FIRST, so a failure part-way can never
    strand a copy of the config outside .backups' retention. True when the
    old dir is gone afterwards."""
    install_dir = Path(install_dir)
    old_dir = install_dir / old_name
    try:
        (old_dir / CONFIG_SNAPSHOT).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _log(f"could not remove {old_name}/{CONFIG_SNAPSHOT}: {e}")
    if not _lexists(old_dir):
        return True
    dest = install_dir / ".backups" / ts
    try:
        children = list(old_dir.iterdir())
        if children:
            dest.mkdir(parents=True, exist_ok=True)
        for child in children:
            target = dest / child.name
            if _lexists(target):
                _remove(target)
            shutil.move(str(child), str(target))
        old_dir.rmdir()
        return True
    except OSError as e:
        _log(f"could not move {old_name} to .backups/: {e}")
        return False


def commit(install_dir, journal=None):
    """Make a pending update permanent. Idempotent. True when the journal is
    gone. False (nothing moved) for a journal that isn't committable: a
    rollback is in progress or was cut short, or its paths look wrong.
    Raises OSError when the 'committing' state can't be written — nothing
    has moved then, and the caller retries later."""
    with _COMMIT_LOCK:
        install_dir = Path(install_dir)
        j = pending(install_dir) if journal is None else journal
        if not j:
            return True
        if j.get("state") not in ("pending_first_boot", "committing"):
            return False
        if _journal_problem(j):
            return False
        if j.get("state") == "pending_first_boot" and _rollback_cut_short(install_dir, j):
            return False
        if j.get("state") != "committing":
            j = dict(j, state="committing")
            _write_json(install_dir / PENDING, j)     # durable first; raises → retry later
        moved = _old_to_backups(install_dir, j["old_dir"], j["ts"])
        _rmdir_quiet(install_dir / f"{FAILED_PREFIX}{j['ts']}")
        if not moved:
            backup_dir = j["old_dir"]
        elif _is_dir(install_dir / ".backups" / j["ts"]):
            backup_dir = f".backups/{j['ts']}"
        else:
            backup_dir = None             # the old dir was gone (removed by hand)
        try:
            _write_json(install_dir / LAST_RESULT, {
                "result": "applied", "from_version": j.get("from_version"),
                "to_version": j.get("to_version"),
                "backup_dir": backup_dir,
                "pip_warning": j.get("pip_warning"),
                "previous_loads": j.get("previous_loads"),
                "at": time.time(), "acknowledged": False})
        except OSError as e:
            _log(f"could not record the update result: {e}")
        try:
            (install_dir / PENDING).unlink()
        except FileNotFoundError:
            pass
        return True


def commit_if_served(install_dir):
    """A restart the running new version's own UI asks for (settings, a
    config import, a restore) is planned, not a crash: once the new version
    has answered, commit before restarting, so the restart isn't counted as
    a failed start and can't roll back a working release (and its settings).
    The applying process's own restart never qualifies (no answer yet, and
    it is the applier). Returns True when a commit happened."""
    j = pending(install_dir)
    if not j or j.get("state") != "pending_first_boot" or not j.get("served_at"):
        return False
    if j.get("applier_pid") == os.getpid():
        return False
    return bool(commit(install_dir))


def note_served(install_dir, now=time.time):
    """Record the first good answer from the new version (best effort for
    the caller: it may raise OSError)."""
    j = pending(install_dir)
    if j and j.get("state") == "pending_first_boot" and not j.get("served_at"):
        j["served_at"] = now()
        _write_json(Path(install_dir) / PENDING, j)


# ---------- instance identity ----------

def _cmdline_args(pid):
    """/proc/<pid>/cmdline as a list of str; None when unreadable or empty
    (a zombie or a kernel thread has an empty one)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def other_instance_pid(install_dir, pid_file=".tracker.pid"):
    """PID of ANOTHER live Aerodrome serving this install, else None.

    A PID counts only when it is alive, is not us, and /proc says it runs
    <install_dir>/main.py (resolved against its cwd, so the unit's relative
    `main.py` + WorkingDirectory form counts). A stale file naming a reused
    PID (init, the feeder, a shell), a zombie, or a process that is gone or
    won't show its cwd (non-dumpable) reads as 'no instance'.

    A PID we may not signal: when the PID file was written by ANOTHER
    account and the process's (world-readable) cmdline runs a main.py, it
    is that account's instance (status/stop run as a different user); a
    PID file we wrote naming such a PID is stale (a reused PID 1).

    Without /proc at all, a live signalable PID counts, as before."""
    install_dir = Path(install_dir)
    pid_path = install_dir / pid_file
    try:
        txt = pid_path.read_text().strip()
        pid = int(txt) if txt else 0
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except PermissionError:
        try:
            foreign = pid_path.stat().st_uid != os.geteuid()
        except OSError:
            foreign = False
        if not foreign:
            return None
        args = _cmdline_args(pid)
        if args is None:         # hidden (hidepid) or no /proc: assume it is
            return pid
        return pid if any(os.path.basename(a) == "main.py" for a in args) else None
    except OSError:              # ProcessLookupError, …
        return None
    if not os.path.isdir("/proc/self"):
        return pid               # no /proc at all: keep the old behaviour
    args = _cmdline_args(pid)
    if args is None:
        return None              # gone, a zombie, or unreadable
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:              # gone (ENOENT/ESRCH) or non-dumpable (EACCES)
        return None
    want = os.path.realpath(install_dir / "main.py")
    for a in args:
        if os.path.basename(a) == "main.py" and os.path.realpath(os.path.join(cwd, a)) == want:
            return pid
    return None


# ---------- boot ----------

def _log(msg):
    try:
        sys.stderr.write(f"aerodrome update-guard: {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - logging must never raise here
        pass


def _reexec():
    """Start this command again in a fresh interpreter (same PID). Never
    continues on a mixed in-memory state: if execv fails, exit 75 so the
    service manager restarts a clean process."""
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
    try:
        os.execv(sys.executable, [sys.executable] + argv[1:])
    except OSError as e:
        _log(f"re-exec failed ({e}); exiting so the service manager restarts Aerodrome")
        os._exit(75)


def _sweep(install_dir, j):
    """Remove what earlier updates left behind and nothing names any more."""
    named = set()
    if j:
        named = {v for v in (j.get("staging_dir"), j.get("old_dir")) if isinstance(v, str)}
        if isinstance(j.get("ts"), str):
            named.add(FAILED_PREFIX + j["ts"])
    for p in install_dir.glob(STAGING_PREFIX + "*"):
        if p.name not in named:
            shutil.rmtree(p, ignore_errors=True)
    failed = []
    for p in install_dir.glob(FAILED_PREFIX + "*"):
        if p.name in named or not _is_dir(p):
            continue
        _rmdir_quiet(p)                   # made ahead by swap_in, nothing parked
        if _is_dir(p):
            failed.append(p)
    for p in sorted(failed, key=lambda p: p.name, reverse=True)[KEEP_FAILED:]:
        shutil.rmtree(p, ignore_errors=True)
    # Only with no journal at all (an unreadable one is left for inspection,
    # and an old dir may then be the only copy of the previous release): an
    # .update-old-<ts>/ nobody names is from a kill before the first journal
    # write (it holds only the config.yaml copy) or a commit that couldn't
    # move it. Drop the config copy; move the rest into .backups/<ts>/.
    if j is None and not _lexists(install_dir / PENDING):
        for p in install_dir.glob(OLD_PREFIX + "*"):
            ts = p.name[len(OLD_PREFIX):]
            if _is_dir(p) and _TS_RE.match(ts):
                _old_to_backups(install_dir, p.name, ts)
        for n in (PENDING + ".tmp", LAST_RESULT + ".tmp"):
            try:
                (install_dir / n).unlink()
            except OSError:
                pass


def on_boot(install_dir, argv=None, reexec=_reexec, log=_log, now=time.time):
    """The boot guard. NEVER raises: an exception here would stop Aerodrome
    at import, which is exactly what the guard exists to prevent."""
    try:
        return _on_boot(Path(install_dir), sys.argv if argv is None else argv, reexec, log, now)
    except Exception as e:  # noqa: BLE001 - the guard must never be what stops startup
        log(f"update guard error, continuing startup: {e!r}")
        return "error"


def _rollback(install_dir, j, reason, reexec, log):
    log(f"rolling back {j.get('to_version')} -> {j.get('from_version')}: {reason}")
    # restore_config: false = finishing an undo the server started in its
    # own process (the new version never ran; config.yaml is the user's).
    swap_back(install_dir, j, reason=reason, restore_config=j.get("restore_config") is not False)
    reexec()
    return "rolled_back"


def _rollback_if_possible(install_dir, j, reason, reexec, log):
    """A rollback the guard decides on (starts counted, or counting
    impossible). With the previous release's copy gone (.update-old-<ts>/
    removed by hand, say, to free space) there is nothing to put back: a
    'rollback' would only take the new release's added files away and
    record a version that isn't installed. Leave the new release in place
    instead; the journal stays, so it can still confirm itself."""
    if not _is_dir(install_dir / j["old_dir"]):
        log(f"can't roll back to {shown_version(j.get('from_version'))} ({reason}): "
            f"{j['old_dir']}/ is gone, so there is nothing to put back; "
            f"leaving {shown_version(j.get('to_version'))} in place")
        return "no_rollback"
    return _rollback(install_dir, j, reason, reexec, log)


def _set_aside_stale(install_dir, j, live_version, log):
    """The journal is for a version that isn't installed: a release without
    this guard was applied over it (going back below 3.4.147, then forward
    again with that release's updater). Nothing of it is undone — that
    would revert a working release and its settings: its previous-release
    copy goes to .backups/<ts>/ and the journal is renamed for inspection."""
    log(f"the update journal is for {shown_version(j.get('to_version'))} but "
        f"{shown_version(live_version)} is installed; setting it aside "
        f"(nothing is rolled back)")
    _old_to_backups(install_dir, j["old_dir"], j["ts"])
    _rmdir_quiet(install_dir / f"{FAILED_PREFIX}{j['ts']}")
    os.replace(install_dir / PENDING, install_dir / f"{STALE_PREFIX}{j['ts']}.json")
    _fsync_dir(install_dir)
    return "stale"


def _on_boot(install_dir, argv, reexec, log, now):
    if len(argv) < 2 or argv[1] != "start":
        return "skip"              # stop/status/import: never touch install files
    if other_instance_pid(install_dir):
        return "other_instance"    # e.g. `main.py start` by hand beside the service
    j = pending(install_dir)
    _sweep(install_dir, j)
    if j is None:
        if (install_dir / PENDING).exists():
            log(f"unreadable {PENDING}; leaving it for inspection")
        return "none"
    state = j.get("state")
    if state not in STATES:
        log(f"unknown journal state {state!r}; leaving it alone")
        return "unknown"
    problem = _journal_problem(j)
    if problem:
        log(f"the update journal has an unexpected {problem}; leaving it alone")
        return "unknown"
    if state == "committing":
        commit(install_dir, j)
        return "committed"
    if state in ("swapping", "rolling_back"):
        return _rollback(install_dir, j, j.get("rollback_reason")
                         or "the update was interrupted while switching files", reexec, log)
    # pending_first_boot
    if _rollback_cut_short(install_dir, j):
        # A rollback started (its 'rolling_back' write is best effort and
        # may have failed on a full disk) and was cut short: finish it —
        # counting or committing would keep a mix of both versions.
        return _rollback(install_dir, j, j.get("rollback_reason")
                         or "an interrupted rollback was finished", reexec, log)
    live = read_version(install_dir / "VERSION")
    if live and live != str(j.get("to_version") or "").strip():
        return _set_aside_stale(install_dir, j, live, log)
    served = j.get("served_at")
    if served and now() - float(served) >= COMMIT_AFTER_SEC:
        # It answered correctly over 10 minutes ago; only the confirm step
        # got stuck (e.g. disk full). Never roll a healthy release back later.
        commit(install_dir, j)
        return "committed"
    n = int(j.get("failed_starts", 0))
    if n >= MAX_FAILED_STARTS:
        return _rollback_if_possible(install_dir, j, f"{j.get('to_version')} didn't start "
                                     f"({n} attempts)", reexec, log)
    j["failed_starts"] = n + 1
    try:
        _write_json(install_dir / PENDING, j)
    except OSError as e:
        # Renames need no free space; counting is impossible, so without
        # this the guard could never fire.
        return _rollback_if_possible(install_dir, j, f"couldn't record the start attempt "
                                     f"({e})", reexec, log)
    return "counting"
