#!/usr/bin/env python3
"""scripts/check_installers.py — static consistency gate for the shell installers.

Cheap, read-only checks on the parts of the installer scripts that must
stay in step with each other. bump-version.sh runs this as a REQUIRED gate
(no skip flag), and package-release.sh runs it through `bump-version.sh
check`; test_check_installers.py imports the functions and feeds them
fixtures. Stdlib only.

  bash-syntax     every git-tracked *.sh parses (`bash -n`). Outside a git
                  checkout: *.sh and scripts/*.sh.
  pkg-detect      the pkg_detect() function is byte-identical in install.sh
                  and scripts/bootstrap.sh (it is duplicated on purpose so
                  each script works standalone — the copies must not drift).
  sudoers-rules   the rule lines (non-comment, non-blank) of install.sh's
                  `<< SUDOEOF` heredoc are identical to
                  scripts/recover-sudoers.sh's, and both heredocs are opened
                  UNQUOTED (a quoted << 'SUDOEOF' writes a literal ${USER}
                  that visudo rejects); install.sh's FIRST SUDOERS_VERSION
                  marker (the one server.py reads) equals recover-sudoers.sh's;
                  and no marker-looking comment sits above install.sh's heredoc.

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


def func_body(text, name):
    """The `name() {` … `}` block, from its first line to the first `}` in
    column 0 — exactly the span that must stay byte-identical."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    return m.group(0) if m else None


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
    writes ${USER} literally instead of expanding it."""
    m = _heredoc_match(text, tag)
    return bool(m and m.group(1))


def rule_lines(body):
    return [l.rstrip() for l in body.splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def first_marker(text):
    for line in text.splitlines():
        m = MARKER_RE.match(line)
        if m:
            return m.group(1)
    return None


def check_sudoers(root):
    root = Path(root)
    inst, rec = _read(root / "install.sh"), _read(root / "scripts/recover-sudoers.sh")
    hi = heredoc(inst) if inst is not None else None
    hr = heredoc(rec) if rec is not None else None
    if hi is None or hr is None:
        return ["sudoers heredoc (<< SUDOEOF) not found in install.sh "
                "and/or scripts/recover-sudoers.sh"]
    errs = []
    for name, text in (("install.sh", inst), ("scripts/recover-sudoers.sh", rec)):
        if heredoc_quoted(text):
            errs.append(f"the sudoers heredoc in {name} is quoted (<< 'SUDOEOF') — ${{USER}} "
                        f"would be written literally and visudo rejects it; both files must "
                        f"use an unquoted << SUDOEOF")
    ri, rr = rule_lines(hi), rule_lines(hr)
    if ri != rr:
        d = list(difflib.unified_diff(ri, rr, "install.sh", "recover-sudoers.sh",
                                      lineterm="", n=0))
        errs.append("sudoers rule lines differ:\n      " + "\n      ".join(d[:20]))
    mi, mr = first_marker(inst), first_marker(hr)
    if mi is None or mi != mr:
        errs.append(f"SUDOERS_VERSION: install.sh's first marker is {mi}, "
                    f"recover-sudoers.sh's is {mr}")
    if first_marker(inst) != first_marker(hi):
        errs.append("a SUDOERS_VERSION-looking comment precedes install.sh's heredoc "
                    "(server.py reads the first one in the installed file's source)")
    return errs


CHECKS = [
    ("bash-syntax", check_bash_syntax),
    ("pkg-detect", check_pkg_detect),
    ("sudoers-rules", check_sudoers),
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
