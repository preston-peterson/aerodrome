#!/usr/bin/env python3
"""scripts/check_installers.py — static consistency gate for the shell installers
and Aerodrome's privilege boundary.

Cheap, read-only checks on the parts of the installer scripts, the system
helper and the app that must stay in step with each other. bump-version.sh runs this as a REQUIRED gate
(no skip flag), and package-release.sh runs it through `bump-version.sh
check`; test_check_installers.py imports the functions and feeds them
fixtures. Stdlib only.

  bash-syntax       every git-tracked *.sh parses (`bash -n`). Outside a git
                    checkout: *.sh and scripts/*.sh.
  pkg-detect        the pkg_detect() and selinux_blocks_home() functions are
                    byte-identical in install.sh and scripts/bootstrap.sh (they
                    are duplicated on purpose so each script works standalone —
                    the copies must not drift).
  recover-delegates install.sh is the ONE place the sudoers rules live:
                    scripts/recover-sudoers.sh carries no rule line and no
                    SUDOERS_VERSION marker and hands off to
                    `install.sh --privileges-only` on a code line (a comment
                    or a grep for the flag doesn't count); install.sh's FIRST
                    marker (the one server.py reads from a staged install.sh)
                    is the first line of render_sudoers()'s << SUDOEOF
                    heredoc — exactly what install.sh writes — and that
                    heredoc is opened UNQUOTED (a quoted << 'SUDOEOF' writes a
                    literal ${u} that visudo rejects).
  sudo-user         in every *.sh, SUDO_USER (and SUDO_UID/SUDO_GID) is read
                    only where the script runs as root. A non-root run can
                    inherit it (`sudo -iu <account> bash …` leaves the
                    admin's name there), so an unguarded ${SUDO_USER:-…}
                    picks the wrong account. A root test ("$(id -u)" or $EUID
                    -eq/= 0) guards a read only when the read can't run
                    without it passing: earlier on the read's line and joined
                    to it by && alone, or as the condition of an if/elif
                    whose then-branch holds the read — on the same line up to
                    that if's own else/elif/fi, or on the lines below it,
                    found by indentation (an else branch never counts). An
                    ||, a test's -o, a `!` before the test or a `;` between
                    them means it doesn't. An unquoted heredoc's body counts
                    where its << is. A line that is safe anyway can say
                    `# sudo-user-ok: <why>`. Cheap, not a parser: when unsure
                    it reports (a root test made in another function, a
                    `[ … ] || exit` line above the read, a { …; } group); it
                    trusts the indentation, and it sees $SUDO_* expansions
                    only (not printenv or ${!name}).
  pipefail-readers  in every *.sh that sets pipefail, no reader that can stop
                    early sits on the right of a pipe (grep -q/-m/-l, head,
                    awk … exit): when it stops, the writer dies of SIGPIPE and
                    pipefail turns that into a failure (exit 141) at random.
                    Capture the output first, then match it; or let awk read
                    to the end with a found-flag. A line that is safe anyway
                    can say `# pipefail-ok: <why>`. Heredoc bodies are skipped.
                    Not covered: `sed …q` (too fuzzy to match reliably).
  ntfy-cache        every message cache Aerodrome's ntfy has used is purged: the
                    helper's NTFY_PURGE_DIRS × NTFY_CACHE_NAMES hold the
                    sandboxed unit's (NTFY_UNIT_TEXT's --cache-file, in its
                    StateDirectory=, under /var/lib/private) with SQLite's
                    -wal/-shm, and the older root-run unit's folder; uninstall.sh
                    runs the helper's ntfy-uninstall, and its own purge branch
                    (`if [ "$should_purge_ntfy_data" = true ]` … `else`/`fi`,
                    code lines only) removes every one of those files.
  priv-version      scripts/aerodrome-helper's line 2 (`# PRIV_VERSION = N`), its
                    PRIV_VERSION constant and install.sh's first SUDOERS_VERSION
                    marker are one number.
  priv-bump         against the last vX.Y.Z tag: when any byte of the helper or
                    any rule line render_sudoers() writes (comments aside)
                    changed, SUDOERS_VERSION is higher than the tag's. SKIPPED
                    outside a git checkout and against a tag before v3.4.145.
  sudo-callsites    only privhelper.py builds a sudo command line (a list or
                    tuple starting with "sudo"; test files aside).
  sudoers-boundary  the rules grant no file write: no tee, install, cp, mv, …,
                    and no rm but the one compatibility line.
  ntfy-yml-shared   the shared ntfy-yml block (server.yml reader/renderer, the
                    unit text) is byte-identical in ntfy_installer.py and the
                    helper.
  helper-api-floor  the helper is Python 3.10 syntax, stdlib only, and uses no
                    3.11+ API from a denylist (rmtree(dir_fd=), except*,
                    ExceptionGroup, tomllib, TaskGroup, StrEnum, datetime.UTC,
                    file_digest, contextlib.chdir, typing.Self, …) nor atomic
                    groups / possessive quantifiers. A denylist, not a proof:
                    its tests also run under 3.10.
  helper-not-stamped  bump-version.sh's FILES doesn't list the helper (its line 2
                    is the PRIV_VERSION marker; a stamp would change it every
                    release).

Usage:
  python3 scripts/check_installers.py [ROOT] [check,check,...]
  python3 scripts/check_installers.py --help

ROOT defaults to the repo this file lives in. Exit 0 = clean, 1 = findings,
2 = usage error (e.g. ROOT is not a directory). Prints one ✓/✗ line per
check (– SKIPPED with the reason when a check can't run here), with indented
details.
"""
import ast
import difflib
import re
import subprocess
import sys
import warnings
from pathlib import Path

# Same pattern server.py uses to read the installed rule's version; it takes
# the FIRST match in the file.
MARKER_RE = re.compile(r"\s*#\s*SUDOERS_VERSION\s*:\s*(\d+)")


def tracked(root, pattern, others=False):
    """git-tracked files matching pattern under root when root is the top of
    a git checkout (others: untracked, not ignored ones too — what could be
    committed), else None (an unpacked release, a scratch copy)."""
    root = Path(root)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             capture_output=True, check=True).stdout
        if Path(top.decode("utf-8", "surrogateescape").strip()).resolve() == root.resolve():
            out = subprocess.run(["git", "ls-files", "-z", "--cached"]
                                 + (["--others", "--exclude-standard"] if others else [])
                                 + ["--", pattern], cwd=root,
                                 capture_output=True, check=True).stdout
            return [root / p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        pass
    return None


def tracked_sh(root):
    """git-tracked *.sh under root when root is the top of a git checkout;
    otherwise a glob of the two dirs that hold shell scripts."""
    root = Path(root)
    files = tracked(root, "*.sh")
    if files is not None:
        return files
    return sorted(root.glob("*.sh")) + sorted((root / "scripts").glob("*.sh"))


def check_bash_syntax(root):
    root = Path(root)
    files = tracked_sh(root)
    if not files:   # a wrong ROOT must never produce a vacuous ✓
        return [f"no *.sh files found under {root}"]
    errs = []
    for f in files:
        if not f.is_file():          # tracked but deleted in the working tree
            continue
        r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
        if r.returncode:
            errs.append(f"{f.relative_to(root)}: bash -n: {r.stderr.strip()[:200]}")
    return errs


def func_span(text, name):
    """(start, end) offsets of the `name() {` … `}` block, from its first line
    to the first `}` in column 0; None when absent."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    return m.span() if m else None


def func_body(text, name):
    """The `name() {` … `}` block, from its first line to the first `}` in
    column 0 — exactly the span that must stay byte-identical."""
    span = func_span(text, name)
    return text[span[0]:span[1]] if span else None


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


SHARED_DUPLICATES = ("pkg_detect", "selinux_blocks_home")


def check_pkg_detect(root):
    root = Path(root)
    inst, boot = _read(root / "install.sh"), _read(root / "scripts/bootstrap.sh")
    errs = []
    for name in SHARED_DUPLICATES:
        a = func_body(inst, name) if inst is not None else None
        b = func_body(boot, name) if boot is not None else None
        if a is None or b is None:
            errs.append(f"{name}() not found in install.sh and/or scripts/bootstrap.sh")
        elif a == b:
            continue
        else:
            d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "install.sh",
                                          "scripts/bootstrap.sh", lineterm="", n=0))
            errs.append(f"{name}() differs between install.sh and scripts/bootstrap.sh "
                        "(copy one over the other; they must stay byte-identical):\n      "
                        + "\n      ".join(d[:20]))
    return errs


def _heredoc_match(text, tag):
    # Group 1 = the opener's quote (empty when unquoted), group 2 = the body.
    return re.search(rf"<<\s*(['\"]?){tag}\1\n(.*?)^{tag}$", text, re.M | re.S)


def heredoc(text, tag="SUDOEOF"):
    m = _heredoc_match(text, tag)
    return m.group(2) if m else None


def heredoc_quoted(text, tag="SUDOEOF"):
    """True when the heredoc is opened as << 'TAG' / << "TAG" — bash then
    writes ${u} literally instead of expanding it."""
    m = _heredoc_match(text, tag)
    return bool(m and m.group(1))


def first_marker(text):
    for line in text.splitlines():
        m = MARKER_RE.match(line)
        if m:
            return m.group(1)
    return None


def first_marker_offset(text):
    """Offset of the first line server.py would read a version from."""
    pos = 0
    for line in text.splitlines(keepends=True):
        if MARKER_RE.match(line):
            return pos
        pos += len(line)
    return None


# A code line that runs install.sh (or a variable holding its path) with
# --privileges-only: `exec bash "$best" --privileges-only …`.
HANDOFF_RE = re.compile(r'^\s*(?:exec\s+)?bash\s+(?:"[^"]*"|\S+)\s+--privileges-only\b')


def code_lines(text):
    """Lines with comments removed (whole-line `#` and a trailing ` #…`)."""
    out = []
    for line in text.splitlines():
        out.append("" if line.lstrip().startswith("#") else line.split(" #", 1)[0])
    return out


def check_recover_delegates(root):
    root = Path(root)
    inst, rec = _read(root / "install.sh"), _read(root / "scripts/recover-sudoers.sh")
    if inst is None or rec is None:
        return ["install.sh and/or scripts/recover-sudoers.sh not found"]
    errs = []
    rule_lines = [n for n, l in enumerate(rec.splitlines(), 1) if "ALL=(" in l]
    if rule_lines:
        errs.append(f"scripts/recover-sudoers.sh carries sudoers rule text (line "
                    f"{', '.join(map(str, rule_lines[:5]))}) — the rules live only in "
                    f"install.sh's render_sudoers(); recover-sudoers.sh must hand off")
    rec_markers = [n for n, l in enumerate(rec.splitlines(), 1) if MARKER_RE.match(l)]
    if rec_markers:
        errs.append(f"scripts/recover-sudoers.sh has a SUDOERS_VERSION marker (line "
                    f"{rec_markers[0]}) — only install.sh may carry one")
    if not any(HANDOFF_RE.search(l) for l in code_lines(rec)):
        errs.append("scripts/recover-sudoers.sh does not hand off to "
                    "`install.sh --privileges-only` (no code line runs `bash <install.sh> "
                    "--privileges-only …`)")
    span = func_span(inst, "render_sudoers")
    if span is None:
        errs.append("render_sudoers() not found in install.sh")
        return errs
    body = inst[span[0]:span[1]]
    doc = _heredoc_match(body, "SUDOEOF")
    if doc is None:
        errs.append("render_sudoers() in install.sh has no << SUDOEOF heredoc")
    elif doc.group(1):
        errs.append("the sudoers heredoc in install.sh's render_sudoers() is quoted "
                    "(<< 'SUDOEOF') — the account name would be written literally and "
                    "visudo rejects it; use an unquoted << SUDOEOF")
    at = first_marker_offset(inst)
    if at is None:
        errs.append("install.sh has no SUDOERS_VERSION marker")
    elif not span[0] <= at < span[1]:
        line = inst.count("\n", 0, at) + 1
        errs.append(f"install.sh:{line}: the first SUDOERS_VERSION-looking line is not "
                    f"inside render_sudoers() (server.py reads the first one in a staged "
                    f"install.sh)")
    elif doc is not None and at != span[0] + doc.start(2):
        line = inst.count("\n", 0, at) + 1
        errs.append(f"install.sh:{line}: the first SUDOERS_VERSION-looking line must be the "
                    f"first line of render_sudoers()'s << SUDOEOF heredoc — server.py reads "
                    f"the first one, and it must be the marker install.sh writes")
    return errs


# SUDO_USER names whoever typed `sudo`; it only means something when the
# script runs as root. Matched as an expansion ($SUDO_USER, ${SUDO_USER…}).
SUDO_VAR_RE = re.compile(r"(?<!\\)\$\{?SUDO_(?:USER|UID|GID)\b")
ROOT_TEST_RE = re.compile(r'(?:\$\(id -u\)|\$\{?EUID\}?)"?\s*(?:-eq|==?)\s*"?0(?![\w])')
CONTINUED = ("\\", "&&", "||", "|")
# Read on quote-masked text (_mask_quotes): what stands between a root test
# and the code it is meant to guard. A command list starts after ; & | ( { or
# a keyword; `||` (or a test's -o) lets that code run without the test
# passing, a `!` turns the test round, `;`/`&`/`|` end its reach, `then` opens
# its branch, and an else/elif/fi that isn't a nested if's leaves the branch.
_WORD = r"(?<![\w$./-]){}(?![\w./-])"
SEP_RE = re.compile(r";|(?<![&>|<])&(?![&>])|(?<!\|)\|(?!\|)")
LIST_START_RE = re.compile(SEP_RE.pattern + r"|[({]|"
                           + _WORD.format("(?:if|elif|then|else|do|while|until)"))
NOT_RE = re.compile(r"(?<![^\s;&|({])!(?=[\s(\[])")
DASH_O_RE = re.compile(_WORD.format("-o"))
THEN_RE = re.compile(_WORD.format("then"))
BRANCH_RE = re.compile(_WORD.format("(if|elif|else|fi)"))


def _mask_quotes(s):
    """s with every quoted or backslash-escaped character replaced by x —
    same length, so offsets still line up — so that a quoted `;`, `||` or
    keyword is never read as shell syntax."""
    out, q, esc = [], None, False
    for ch in s:
        if esc:
            out.append("x")
            esc = False
        elif ch == "\\" and q != "'":
            out.append(ch)
            esc = True
        elif q:
            out.append(ch if ch == q else "x")
            if ch == q:
                q = None
        elif ch in "'\"":
            out.append(ch)
            q = ch
        else:
            out.append(ch)
    return "".join(out)


def _root_test_guards(masked, test, upto, branch):
    """Whether the root test matched at `test` (offsets into the quote-masked
    logical line `masked`) makes the code at offset `upto` run only as root.
    branch: `upto` is the branch BELOW this line (an if/elif head), not a
    spot on it."""
    before = masked[:test.start()]
    starts = [m.end() for m in LIST_START_RE.finditer(before)]
    pre = before[starts[-1] if starts else 0:]      # the test's own command list
    if "||" in pre or NOT_RE.search(pre) or DASH_O_RE.search(pre):
        return False
    between = masked[test.end():upto]
    close = between.find("]")
    if close >= 0 and DASH_O_RE.search(between[:close]):   # [ … -eq 0 -o … ]
        return False
    then = THEN_RE.search(between)
    cond = between[:then.start()] if then else between
    cond = cond.rstrip()
    if (then or branch) and cond.endswith(";"):
        cond = cond[:-1]
    if "||" in cond or SEP_RE.search(cond):
        return False
    if then:
        depth = 0
        for w in BRANCH_RE.finditer(between[then.end():]):
            if w.group(1) == "if":
                depth += 1
            elif depth == 0:          # this if's else/elif/fi: out of the branch
                return False
            elif w.group(1) == "fi":
                depth -= 1
    return True


def sudo_user_lines(text):
    """[(line_no, line)] of code lines that expand SUDO_USER/UID/GID where
    nothing makes them run only as root. A root test guards a read on its own
    (logical) line when the read can't run unless the test passed: joined to
    it by && alone, or in the then-branch of an if/elif whose condition it is
    (and/and only, not negated) — up to that if's own else/elif/fi. Below an
    if/elif line, the enclosing branches are the less-indented lines above
    the read, nearest first; an `else` (or any other opener) ends that level,
    so the else branch of a root test never counts. An unquoted heredoc
    expands it too: its body lines count where their << is; a quoted one
    (<< 'EOF') doesn't."""
    lines = text.splitlines()
    code, scan, at, doc = [], [], [], None   # doc = (tag, expands, opener)
    for i, line in enumerate(lines):
        if doc is not None:
            code.append("")
            if line.strip() == doc[0]:
                doc = None
                scan.append("")
                at.append(i)
            else:
                scan.append(line if doc[1] else "")
                at.append(doc[2])
            continue
        c = "" if line.lstrip().startswith("#") else line.split(" #", 1)[0]
        code.append(c)
        scan.append(c)
        at.append(i)
        m = HEREDOC_RE.search(c)
        if m:
            doc = (m.group(2), not m.group(1), i)

    def head(j):   # first line of the logical line j belongs to
        while j > 0 and code[j - 1].rstrip().endswith(CONTINUED):
            j -= 1
        return j

    def logical(j):
        """(text, {line index: offset in text}) of j's logical line."""
        j = head(j)
        parts, offs, pos = [], {}, 0
        while True:
            offs[j] = pos
            parts.append(code[j])
            pos += len(code[j]) + 1
            if not (code[j].rstrip().endswith(CONTINUED) and j + 1 < len(code)):
                break
            j += 1
        return " ".join(parts), offs

    def root_guarded(j, upto=None):
        """A root test on j's logical line guards offset `upto` of it, or,
        with upto None, the branch below it (j being an if/elif head)."""
        raw, _ = logical(j)
        masked = _mask_quotes(raw)
        end = len(raw) if upto is None else upto
        return any(_root_test_guards(masked, t, end, upto is None)
                   for t in ROOT_TEST_RE.finditer(raw[:end]))

    def indent(s):
        return len(s) - len(s.lstrip())

    hits = []
    for i, seen in enumerate(scan):
        k = at[i]
        reads = list(SUDO_VAR_RE.finditer(seen))
        if not reads or "sudo-user-ok" in lines[i] + lines[k]:
            continue
        _, offs = logical(k)
        if k == i:
            spots = [offs[k] + r.start() for r in reads]
        else:            # a heredoc body runs where its << is
            spots = [offs[k] + HEREDOC_RE.search(code[k]).start()]
        if all(root_guarded(k, s) for s in spots):
            continue
        guarded, j = False, head(k)
        level = indent(code[j])
        while j > 0 and level > 0 and not guarded:
            j -= 1
            s = code[j].strip()
            if not s or s in ("then", "do"):
                continue
            h = head(j)
            if indent(code[h]) >= level:
                continue
            level = indent(code[h])
            if re.match(r"(?:if|elif)\b", code[h].lstrip()):
                guarded = root_guarded(h)
            j = h
        if not guarded:
            hits.append((i + 1, lines[i].strip()))
    return hits


def check_sudo_user(root):
    root = Path(root)
    errs = []
    for f in tracked_sh(root):
        text = _read(f)
        if text is None:
            continue
        for n, line in sudo_user_lines(text):
            errs.append(f"{f.relative_to(root)}:{n}: SUDO_USER read without a root check — a "
                        f"non-root run can inherit it (`sudo -iu <account> bash …` leaves the "
                        f"admin's name there) and pick the wrong account. Read it only under "
                        f"`if [ \"$(id -u)\" -eq 0 ]`, or mark '# sudo-user-ok: <why>'"
                        f"\n        {line[:120]}")
    return errs


# A reader that may stop before its input ends, on the right of a single |.
READER_RE = re.compile(
    r"(?<!\|)\|(?!\|)\s*("
    r"grep\b[^|;]*\s(?:-[A-Za-z0-9]*[qml]|--(?:quiet|silent|max-count|files-with-matches)\b)"
    r"|head\b"
    r"|awk\b[^|]*\bexit\b)")
PIPEFAIL_RE = re.compile(r"^\s*set\s+(?:-[A-Za-z]*o\s+pipefail|-o\s+pipefail|.*\s-o\s+pipefail)",
                         re.M)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def pipefail_reader_lines(text):
    """[(line_no, line)] of early-exit readers after a pipe, for a script
    that sets pipefail; [] for one that doesn't."""
    if not PIPEFAIL_RE.search(text):
        return []
    hits, in_doc, prev_pipe = [], None, False
    for n, line in enumerate(text.splitlines(), 1):
        if in_doc is not None:
            if line.strip() == in_doc:    # also matches a <<- tab-indented end
                in_doc = None
            continue
        stripped = line.lstrip()
        code = "" if stripped.startswith("#") else line.split(" #", 1)[0]
        probe = ("| " + code) if prev_pipe else code
        if code and READER_RE.search(probe) and "pipefail-ok" not in line:
            hits.append((n, line.strip()))
        m = HEREDOC_RE.search(code)
        if m:
            in_doc = m.group(2)
        tail = code.rstrip()
        prev_pipe = bool(code) and tail.endswith("|") and not tail.endswith("||")
    return hits


def check_pipefail_readers(root):
    root = Path(root)
    errs = []
    for f in tracked_sh(root):
        text = _read(f)
        if text is None:
            continue
        for n, line in pipefail_reader_lines(text):
            errs.append(f"{f.relative_to(root)}:{n}: early-exit reader after a pipe under "
                        f"pipefail (SIGPIPE → 141): capture first, then match; or mark "
                        f"'# pipefail-ok: <why>'\n        {line[:120]}")
    return errs


def _names_path(text, path):
    """True when `path` occurs in text as a whole path (cache.db must not be
    satisfied by cache.db-wal, nor by a glob such as cache.db*)."""
    return re.search(re.escape(path) + r"(?![\w.*-])", text) is not None


PURGE_IF_RE = re.compile(r'^(\s*)if \[ "\$should_purge_ntfy_data" = true \]; then\s*$')


def ntfy_purge_block(text):
    """The code lines of uninstall.sh's `if [ "$should_purge_ntfy_data" = true ];
    then` branch, up to its `else`/`elif`/`fi` at the same indent, joined;
    None when there is no such branch. Only these lines remove the cache: the
    size prompt's `du`, an echo or a comment elsewhere name the same paths."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = PURGE_IF_RE.match(line)
        if not m:
            continue
        end = re.compile(re.escape(m.group(1)) + r"(?:else|elif\b.*|fi)\s*$")
        for j in range(i + 1, len(lines)):
            if end.match(lines[j]):
                return "\n".join(code_lines("\n".join(lines[i + 1:j])))
        return None
    return None


UNIT_CACHE_RE = re.compile(r"--cache-file (/var/lib/([A-Za-z0-9_.-]+)/([^\s/]+))")


def check_ntfy_cache(root):
    """Every message cache Aerodrome's ntfy has used gets purged. The helper's
    purge list (NTFY_PURGE_DIRS × NTFY_CACHE_NAMES) holds the sandboxed
    unit's cache — NTFY_UNIT_TEXT's --cache-file, in its StateDirectory=,
    which DynamicUser keeps under /var/lib/private — with SQLite's -wal/-shm,
    and the older root-run unit's folder (the shared block's first
    NTFY_CACHE_FILES entry); uninstall.sh runs the helper's ntfy-uninstall,
    and its own purge (for a system without the helper) removes each of the
    same files. Fails closed when a constant moved."""
    root = Path(root)
    helper, unin = _read(root / HELPER_REL), _read(root / "uninstall.sh")
    if helper is None or unin is None:
        return [f"{HELPER_REL} and/or uninstall.sh not found"]
    dirs, names = (_module_constant(helper, "NTFY_PURGE_DIRS"),
                   _module_constant(helper, "NTFY_CACHE_NAMES"))
    unit, files = (_module_constant(helper, "NTFY_UNIT_TEXT"),
                   _module_constant(helper, "NTFY_CACHE_FILES"))
    if not (isinstance(dirs, tuple) and isinstance(names, tuple) and isinstance(unit, str)
            and isinstance(files, tuple) and files):
        return [f"{HELPER_REL}: NTFY_PURGE_DIRS, NTFY_CACHE_NAMES, NTFY_UNIT_TEXT or "
                f"NTFY_CACHE_FILES isn't a literal there any more — update this check alongside them"]
    errs = []
    m = UNIT_CACHE_RE.search(unit)
    state = re.search(r"(?m)^StateDirectory=(\S+)$", unit)
    if not m or not state or m.group(2) != state.group(1):
        errs.append(f"{HELPER_REL}: NTFY_UNIT_TEXT's --cache-file isn't in its StateDirectory=")
    else:
        for d in ("/var/lib/private/" + state.group(1), files[0].rsplit("/", 1)[0]):
            if d not in dirs:
                errs.append(f"{HELPER_REL}: NTFY_PURGE_DIRS lacks {d} (a cache Aerodrome's ntfy used)")
        base = m.group(3)
        for n in (base, base + "-wal", base + "-shm"):
            if n not in names:
                errs.append(f"{HELPER_REL}: NTFY_CACHE_NAMES lacks {n}")
    if not any(re.search(r'"\$HELPER" ntfy-uninstall(?![\w-])', l) for l in code_lines(unin)):
        errs.append('uninstall.sh never runs the helper\'s ntfy-uninstall ("$HELPER" ntfy-uninstall)')
    block = ntfy_purge_block(unin)
    if block is None:    # fail closed: a reworded branch must be re-targeted, not pass
        errs.append('uninstall.sh: no `if [ "$should_purge_ntfy_data" = true ]; then` '
                    "… `else`/`fi` branch found to look for its own purge in — update "
                    "this check alongside it")
    else:
        for d in dirs:
            for n in names:
                if not _names_path(block, f"{d}/{n}"):
                    errs.append(f"uninstall.sh's own purge never removes {d}/{n} (the helper's does)")
    return errs


HELPER_REL = "scripts/aerodrome-helper"
SHARED_BEGIN = "# --- BEGIN shared ntfy-yml block"
SHARED_END = "# --- END shared ntfy-yml block ---"
PRIV_LINE_RE = re.compile(r"# PRIV_VERSION = (\d+)\Z")


class Skipped(Exception):
    """A check that can't run here (no git, no tag before it): main()
    prints SKIPPED with the reason."""


def _module_constant(src, name):
    """The literal value of a module-level `name = <literal>` in src, or
    None (not there, or not a literal)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def check_priv_version(root):
    """The helper's line 2 (`# PRIV_VERSION = N`, which the app and install.sh
    read without running it), its PRIV_VERSION constant and install.sh's first
    SUDOERS_VERSION marker are one number."""
    root = Path(root)
    helper, inst = _read(root / HELPER_REL), _read(root / "install.sh")
    if helper is None or inst is None:
        return [f"{HELPER_REL} and/or install.sh not found"]
    lines = helper.splitlines()
    m = PRIV_LINE_RE.match(lines[1]) if len(lines) > 1 else None
    if not m:
        return [f"{HELPER_REL}: line 2 must be exactly `# PRIV_VERSION = N`"]
    const = _module_constant(helper, "PRIV_VERSION")
    marker = first_marker(inst)
    if not (str(const) == m.group(1) == marker):
        return [f"{HELPER_REL} line 2 says {m.group(1)}, its PRIV_VERSION is {const} and "
                f"install.sh's first SUDOERS_VERSION is {marker} — they must be one number"]
    return []


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True)


def rendered_sudoers(install_text):
    """What render_sudoers() writes for an invented account (example-svc),
    from the function alone run in bash -c; None when it doesn't run."""
    body = func_body(install_text, "render_sudoers")
    if body is None:
        return None
    r = subprocess.run(["bash", "-c", body + 'render_sudoers example-svc\n'],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return None if r.returncode else r.stdout


def rendered_rule_lines(install_text):
    """The rule lines render_sudoers() writes for an invented account (comments
    and blank lines left out)."""
    out = rendered_sudoers(install_text)
    if out is None:
        return None
    return [l for l in out.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def check_priv_bump(root):
    """Against the last release tag (vX.Y.Z reachable from HEAD): if any byte
    of the helper changed (the shared ntfy-yml block included) or the rule
    lines render_sudoers() writes did (comments don't count), install.sh's
    SUDOERS_VERSION must be higher than the tag's — users then get the
    "Sudoers update required" prompt, the only way a new helper or new rules
    reach a system (the in-app update runs as the service account)."""
    root = Path(root)
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.decode().strip()).resolve() != root.resolve():
        raise Skipped("not a git checkout (an unpacked release): no tag to compare with")
    tag = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*")
    if tag.returncode:
        raise Skipped("no vX.Y.Z tag reachable from HEAD")
    tag = tag.stdout.decode().strip()
    old_inst = _git(root, "show", f"{tag}:install.sh")
    old_inst = old_inst.stdout.decode("utf-8", "replace") if not old_inst.returncode else ""
    if func_body(old_inst, "render_sudoers") is None:
        raise Skipped(f"{tag}'s install.sh has no render_sudoers() (before v3.4.145)")
    old_helper = _git(root, "show", f"{tag}:{HELPER_REL}")
    old_helper = old_helper.stdout if not old_helper.returncode else None
    try:
        new_helper = (root / HELPER_REL).read_bytes()
    except OSError:
        new_helper = None
    inst = _read(root / "install.sh")
    old_rules, new_rules = rendered_rule_lines(old_inst), rendered_rule_lines(inst or "")
    if new_rules is None:
        return ["install.sh: render_sudoers() didn't run (bash -c) — can't compare its rules"]
    changed = []
    if old_helper != new_helper:
        changed.append(f"{HELPER_REL} changed")
    if old_rules != new_rules:
        changed.append("the rule lines render_sudoers() writes changed")
    old_mark, new_mark = int(first_marker(old_inst) or 0), int(first_marker(inst or "") or 0)
    if changed and not new_mark > old_mark:
        return [f"since {tag}: {' and '.join(changed)}, but SUDOERS_VERSION is still "
                f"{new_mark} ({tag}: {old_mark}) — raise it (and the helper's PRIV_VERSION, "
                f"line 2 too), or nobody's system gets the change"]
    return []


def check_sudo_callsites(root):
    """Only privhelper.py builds a sudo command line: no other first-party
    Python file (tests aside) has a list or tuple literal starting with
    "sudo". Everything privileged goes through the helper or run_sudo()."""
    root = Path(root)
    files = tracked(root, "*.py", others=True)
    if files is None:
        files = sorted(p for p in root.rglob("*.py") if "venv" not in p.relative_to(root).parts)
    errs = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        if rel == "privhelper.py" or f.name.startswith("test_"):
            continue
        src = _read(f)
        if src is None:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)   # e.g. "\\/" in a string
                tree = ast.parse(src)
        except SyntaxError:
            continue          # bump-version.sh's syntax gate reports it
        for node in ast.walk(tree):
            if (isinstance(node, (ast.List, ast.Tuple)) and node.elts
                    and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "sudo"):
                errs.append(f"{rel}:{node.lineno}: a sudo command line outside privhelper.py — "
                            f"use privhelper.run_helper() (or run_sudo() for the restart/cat rules)")
    return errs


ALLOWED_RM = "/bin/rm -f /etc/systemd/system/aerodrome-synthetic-feeder.service"
# Every command the rules grant, exactly — all as root, NOPASSWD. Adding one
# (or a wildcard, an alias, a Defaults line) is a decision made here too.
SUDOERS_ALLOWED = (
    "/usr/local/libexec/aerodrome/aerodrome-helper",
    "/bin/systemctl restart aerodrome", "/usr/bin/systemctl restart aerodrome",
    "/bin/systemctl restart --no-block aerodrome", "/usr/bin/systemctl restart --no-block aerodrome",
    "/bin/cat /etc/sudoers.d/aerodrome", "/usr/bin/cat /etc/sudoers.d/aerodrome",
    # the previous release's switch-to-real (compatibility; gone at SUDOERS_VERSION 7)
    "/bin/systemctl stop aerodrome-synthetic-feeder",
    "/usr/bin/systemctl stop aerodrome-synthetic-feeder",
    "/bin/systemctl disable aerodrome-synthetic-feeder",
    "/usr/bin/systemctl disable aerodrome-synthetic-feeder",
    ALLOWED_RM, "/bin/systemctl daemon-reload", "/usr/bin/systemctl daemon-reload",
)
RULE_LINE_RE = re.compile(r"example-svc ALL=\(root\) NOPASSWD: (\S.*)\Z")
WRITERS = ("tee", "install", "cp", "mv", "dd", "chmod", "chown", "ln", "rm")


def check_sudoers_boundary(root):
    """The rules render_sudoers() writes grant exactly SUDOERS_ALLOWED, each
    as root with NOPASSWD: plain rule lines only (no Defaults, aliases or
    includes — `#include` is a directive, not a comment), and no wildcard,
    negation, ALL or sudoedit in a command. A command that writes files
    (tee, install, rm but the compatibility line, …) is named as such: only
    the system helper writes, after its checks."""
    inst = _read(Path(root) / "install.sh")
    out = rendered_sudoers(inst or "")
    if out is None:
        return ["install.sh: render_sudoers() not found or didn't run"]
    where, errs, granted = "install.sh render_sudoers()", [], []
    for line in out.splitlines():
        if re.match(r"\s*[#@]include", line):
            errs.append(f"{where}: `{line.strip()}` — no includes in Aerodrome's rules")
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = RULE_LINE_RE.match(line)
        if not m:
            errs.append(f"{where}: `{line.strip()}` — every rule line is "
                        f"`<account> ALL=(root) NOPASSWD: <commands>`")
            continue
        granted += [c.strip() for c in m.group(1).split(",")]
    for cmd in granted:
        name = (cmd.split() or [""])[0].rsplit("/", 1)[-1]
        if re.search(r"[*?\[\]!\\]|\bALL\b|sudoedit", cmd):
            errs.append(f"{where}: `{cmd}` — no wildcards, negation, ALL or sudoedit")
        elif name in WRITERS and cmd != ALLOWED_RM:
            errs.append(f"{where}: `{cmd}` — the rules may not grant writing files "
                        f"(only the system helper writes, after its checks)")
        elif cmd not in SUDOERS_ALLOWED:
            errs.append(f"{where}: `{cmd}` isn't one of the commands Aerodrome's rules grant "
                        f"(scripts/check_installers.py SUDOERS_ALLOWED)")
    for cmd in SUDOERS_ALLOWED:
        if cmd not in granted:
            errs.append(f"{where}: no longer grants `{cmd}` — if that is meant, take it out of "
                        f"SUDOERS_ALLOWED too")
    return errs


def shared_block(text):
    """The text from the BEGIN marker line to the END marker line, or None."""
    a = text.find(SHARED_BEGIN)
    b = text.find(SHARED_END, a)
    return text[a:b + len(SHARED_END)] if a >= 0 and b >= 0 else None


def check_ntfy_yml_shared(root):
    """ntfy_installer.py and the helper carry one byte-identical copy of the
    shared ntfy-yml block (the server.yml reader/renderer and the unit text):
    what the app checks is what root writes."""
    root = Path(root)
    a = shared_block(_read(root / "ntfy_installer.py") or "")
    b = shared_block(_read(root / HELPER_REL) or "")
    if a is None or b is None:
        return [f"the shared ntfy-yml block markers are missing in ntfy_installer.py "
                f"and/or {HELPER_REL}"]
    if a == b:
        return []
    d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "ntfy_installer.py",
                                  HELPER_REL, lineterm="", n=0))
    return [f"the shared ntfy-yml block differs between ntfy_installer.py and {HELPER_REL} "
            f"(copy one over the other):\n      " + "\n      ".join(d[:20])]


# Python 3.11+ names the helper must not use (it runs under any system
# Python from 3.10). A denylist, not a proof: test_aerodrome_helper.py also
# runs under 3.10.
FLOOR_ATTRS = {("shutil", "rmtree"): "dir_fd", ("asyncio", "TaskGroup"): None,
               ("enum", "StrEnum"): None, ("datetime", "UTC"): None,
               ("hashlib", "file_digest"): None, ("contextlib", "chdir"): None,
               ("typing", "Self"): None, ("sys", "exception"): None, ("re", "NOFLAG"): None,
               ("operator", "call"): None}
FLOOR_NAMES = {"ExceptionGroup", "BaseExceptionGroup"}
FLOOR_MODULES = {"tomllib"}
POSSESSIVE_RE = re.compile(r"\(\?>|(?<!\\)[*+?}]\+")


def check_helper_api_floor(root):
    """The helper parses as Python 3.10, imports the standard library only,
    and uses none of the 3.11+ APIs above — however it spells them: `mod.X`,
    an aliased `import mod as m` then `m.X`, or `from mod import X` (as Y) —
    nor atomic groups or possessive quantifiers in its regexes."""
    src = _read(Path(root) / HELPER_REL)
    if src is None:
        return [f"{HELPER_REL} not found"]
    try:
        tree = ast.parse(src, feature_version=(3, 10))
    except SyntaxError as e:
        return [f"{HELPER_REL}:{e.lineno}: not Python 3.10 syntax: {e.msg}"]
    errs, std = [], set(getattr(sys, "stdlib_module_names", ()))
    mods, names = {}, {}      # local name → module; → (module, name) it was imported from
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                names[a.asname or a.name] = (node.module, a.name)
    for node in ast.walk(tree):
        mods_of = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                   [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
        for mod in mods_of:
            top = mod.split(".")[0]
            if top in FLOOR_MODULES or (std and top not in std):
                errs.append(f"{HELPER_REL}:{node.lineno}: imports {mod} (stdlib 3.10 only)")
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if FLOOR_ATTRS.get((node.module, a.name), "") is None:
                    errs.append(f"{HELPER_REL}:{node.lineno}: {node.module}.{a.name} is Python 3.11+")
        if type(node).__name__ == "TryStar":
            errs.append(f"{HELPER_REL}:{node.lineno}: except* is Python 3.11+")
        if isinstance(node, ast.Name) and node.id in FLOOR_NAMES:
            errs.append(f"{HELPER_REL}:{node.lineno}: {node.id} is Python 3.11+")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            key = (mods.get(node.value.id, node.value.id), node.attr)
            if FLOOR_ATTRS.get(key, "") is None:
                errs.append(f"{HELPER_REL}:{node.lineno}: {key[0]}.{key[1]} is Python 3.11+")
        if isinstance(node, ast.Call):
            f = node.func
            key = ((mods.get(f.value.id, f.value.id), f.attr)
                   if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                   else names.get(f.id) if isinstance(f, ast.Name) else None)
            kw = FLOOR_ATTRS.get(key) if key else None
            if kw and any(k.arg == kw for k in node.keywords):
                errs.append(f"{HELPER_REL}:{node.lineno}: {key[0]}.{key[1]}({kw}=) is Python 3.11+")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and POSSESSIVE_RE.search(node.value):
            errs.append(f"{HELPER_REL}:{node.lineno}: an atomic group or possessive quantifier "
                        f"(regex, Python 3.11+)")
    return errs


def check_helper_not_stamped(root):
    """bump-version.sh never writes a `# Version:` line into the helper: its
    line 2 is the PRIV_VERSION marker, and any byte it changes is a
    permissions change (priv-bump)."""
    text = _read(Path(root) / "bump-version.sh")
    if text is None:
        return ["bump-version.sh not found"]
    m = re.search(r"^FILES=\((.*?)^\)", text, re.M | re.S)
    if not m:
        return ["bump-version.sh: no FILES=( … ) list found — update this check alongside it"]
    if re.search(r'["\']?' + re.escape(HELPER_REL) + r'["\']?\s', m.group(1) + "\n"):
        return [f"bump-version.sh FILES lists {HELPER_REL} — take it out (line 2 is reserved "
                f"for PRIV_VERSION; a version stamp would change the helper every release)"]
    return []


CHECKS = [
    ("bash-syntax", check_bash_syntax),
    ("pkg-detect", check_pkg_detect),
    ("recover-delegates", check_recover_delegates),
    ("sudo-user", check_sudo_user),
    ("pipefail-readers", check_pipefail_readers),
    ("ntfy-cache", check_ntfy_cache),
    ("priv-version", check_priv_version),
    ("priv-bump", check_priv_bump),
    ("sudo-callsites", check_sudo_callsites),
    ("sudoers-boundary", check_sudoers_boundary),
    ("ntfy-yml-shared", check_ntfy_yml_shared),
    ("helper-api-floor", check_helper_api_floor),
    ("helper-not-stamped", check_helper_not_stamped),
]


def main(argv):
    if argv[1:2] in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    if not root.is_dir():
        print(f"  ✗ ROOT is not a directory: {root}")
        print("  Usage: python3 scripts/check_installers.py [ROOT] [check,check,...]  (--help)")
        return 2
    known = [n for n, _ in CHECKS]
    names = argv[2].split(",") if len(argv) > 2 else known
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"  ✗ unknown check(s): {', '.join(unknown)} (known: {', '.join(known)})")
        return 1
    bad = 0
    for name, fn in CHECKS:
        if name not in names:
            continue
        try:
            errs = fn(root)
        except Skipped as e:
            print(f"  – {name} SKIPPED: {e}")
            continue
        print(f"  {'✓' if not errs else '✗'} {name}")
        for e in errs:
            print(f"      {e}")
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
