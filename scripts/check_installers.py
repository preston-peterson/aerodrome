#!/usr/bin/env python3
"""scripts/check_installers.py — static consistency gate for the shell installers.

Cheap, read-only checks on the parts of the installer scripts that must
stay in step with each other. bump-version.sh runs this as a REQUIRED gate
(no skip flag), and package-release.sh runs it through `bump-version.sh
check`; test_check_installers.py imports the functions and feeds them
fixtures. Stdlib only.

  bash-syntax       every git-tracked *.sh parses (`bash -n`). Outside a git
                    checkout: *.sh and scripts/*.sh.
  pkg-detect        the pkg_detect() function is byte-identical in install.sh
                    and scripts/bootstrap.sh (it is duplicated on purpose so
                    each script works standalone — the copies must not drift).
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
  ntfy-cache        the cache-file path ntfy_installer.py writes into ntfy's
                    server.yml is removed by uninstall.sh (on a code line, not
                    only named in a comment).

Usage:
  python3 scripts/check_installers.py [ROOT] [check,check,...]
  python3 scripts/check_installers.py --help

ROOT defaults to the repo this file lives in. Exit 0 = clean, 1 = findings,
2 = usage error (e.g. ROOT is not a directory). Prints one ✓/✗ line per
check, with indented details.
"""
import difflib
import re
import subprocess
import sys
from pathlib import Path

# Same pattern server.py uses to read the installed rule's version; it takes
# the FIRST match in the file.
MARKER_RE = re.compile(r"\s*#\s*SUDOERS_VERSION\s*:\s*(\d+)")


def tracked_sh(root):
    """git-tracked *.sh under root when root is the top of a git checkout;
    otherwise (an unpacked release, a scratch copy) a glob of the two dirs
    that hold shell scripts."""
    root = Path(root)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             capture_output=True, check=True).stdout
        if Path(top.decode("utf-8", "surrogateescape").strip()).resolve() == root.resolve():
            out = subprocess.run(["git", "ls-files", "-z", "--", "*.sh"], cwd=root,
                                 capture_output=True, check=True).stdout
            return [root / p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        pass
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


def check_pkg_detect(root):
    root = Path(root)
    inst, boot = _read(root / "install.sh"), _read(root / "scripts/bootstrap.sh")
    a = func_body(inst, "pkg_detect") if inst is not None else None
    b = func_body(boot, "pkg_detect") if boot is not None else None
    if a is None or b is None:
        return ["pkg_detect() not found in install.sh and/or scripts/bootstrap.sh"]
    if a == b:
        return []
    d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "install.sh",
                                  "scripts/bootstrap.sh", lineterm="", n=0))
    return ["pkg_detect() differs between install.sh and scripts/bootstrap.sh "
            "(copy one over the other; they must stay byte-identical):\n      "
            + "\n      ".join(d[:20])]


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


def check_ntfy_cache(root):
    root = Path(root)
    src, unin = _read(root / "ntfy_installer.py"), _read(root / "uninstall.sh")
    if src is None or unin is None:
        return ["ntfy_installer.py and/or uninstall.sh not found"]
    m = re.search(r"^cache-file:\s*['\"]?(/[^\s'\"]+)", src, re.M)
    if not m:    # fail closed: a moved/reworded template must be re-targeted, not pass
        return ["ntfy_installer.py: no `cache-file: /absolute/path` line found in the "
                "server.yml template — update this check alongside it"]
    path = m.group(1)
    code = "\n".join(l for l in unin.splitlines() if not l.lstrip().startswith("#"))
    if path in code:
        return []
    return [f"uninstall.sh never removes ntfy's cache file {path} "
            f"(ntfy_installer.py's server.yml puts it there)"]


CHECKS = [
    ("bash-syntax", check_bash_syntax),
    ("pkg-detect", check_pkg_detect),
    ("recover-delegates", check_recover_delegates),
    ("sudo-user", check_sudo_user),
    ("pipefail-readers", check_pipefail_readers),
    ("ntfy-cache", check_ntfy_cache),
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
        errs = fn(root)
        print(f"  {'✓' if not errs else '✗'} {name}")
        for e in errs:
            print(f"      {e}")
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
