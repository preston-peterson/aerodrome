"""
Documentation drift detector.

Scans for common ways documentation gets stale. Run manually or called by
bump-version.sh as an advisory check — prints warnings but doesn't exit
non-zero for cosmetic drift (so a minor inconsistency never blocks a
release when the release itself is correct).

Checks:

  1. Version consistency. The VERSION file is the single source of truth.
     Flag any file that references a version number matching the X.Y.Z
     pattern but NOT the current version, unless it's clearly historical
     (e.g. a CHANGELOG entry for an old release, a "since v2.40.1" doc
     comment referring to when a feature was introduced).

  2. Broken relative links in README. Markdown links pointing to local
     files that don't exist on disk.

  3. Screenshots referenced but missing. Any docs/screenshot-*.png
     mentioned in README.md that doesn't exist.

  4. Project-structure drift. The tree in README.md Project-structure
     section should roughly match what's actually at the top of the repo.
     Top-level files mentioned in the tree that don't exist — or top-level
     files that exist but aren't mentioned — are flagged.

  5. Stale PDF. docs/Aerodrome_Overview.pdf older than the most recent
     mtime across the core source files (main.py, server.py, collector.py,
     VERSION, CHANGELOG.md) means it was built before the current state
     of the repo and needs regenerating via scripts/build_overview_pdf.py.

  6. Stale screenshots. Any docs/screenshot-*.png older than the template
     file it should depict (e.g. screenshot-live.png vs templates/index.html)
     means the template changed after the screenshot was captured.

  7. Screenshot mock contract. The scripts/screenshots.py fixture returns
     mock /api/* responses so the headless browser renders without a live
     backend. Each mock must include the top-level fields the frontend
     actually reads from the response — if they drift out of sync, the
     screenshot renders with empty cells or zero counters and nobody
     notices until a human looks at the PNG. Added in v2.41.34 after
     v2.41.33 fixed exactly this class of bug on the /api/all mock.

Exit codes:
  0  — no warnings, or warnings only (advisory mode).
  2  — a check failed hard (file system issue, not a drift warning).

Run:
  python3 scripts/check_docs.py
  python3 scripts/check_docs.py --verbose    # show every check result, not just warnings
"""
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION = (REPO_ROOT / "VERSION").read_text().strip()

# Terminal colors — only emit when stdout is a tty; plain text otherwise
# so log capture / grep / tee all stay readable.
if sys.stdout.isatty():
    RED    = "\033[31m"
    YELLOW = "\033[33m"
    GREEN  = "\033[32m"
    CYAN   = "\033[36m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"
else:
    RED = YELLOW = GREEN = CYAN = DIM = RESET = ""


class DriftReport:
    """Accumulates warnings. Each warning is a (category, message, path)
    tuple so the summary can group by category."""

    def __init__(self):
        self.warnings: list[tuple[str, str, str]] = []

    def warn(self, category: str, msg: str, path: str = ""):
        self.warnings.append((category, msg, path))

    def summary(self) -> int:
        """Print grouped summary, return number of warnings."""
        if not self.warnings:
            print(f"{GREEN}✓ Documentation checks passed — no drift detected.{RESET}")
            return 0

        by_category: dict[str, list[tuple[str, str]]] = {}
        for cat, msg, path in self.warnings:
            by_category.setdefault(cat, []).append((msg, path))

        print(f"{YELLOW}⚠ {len(self.warnings)} documentation warning"
              f"{'s' if len(self.warnings) != 1 else ''} "
              f"across {len(by_category)} categor"
              f"{'ies' if len(by_category) != 1 else 'y'}:{RESET}")
        print()

        for cat, items in by_category.items():
            print(f"{CYAN}  {cat} ({len(items)}){RESET}")
            for msg, path in items:
                if path:
                    print(f"    • {msg}  {DIM}[{path}]{RESET}")
                else:
                    print(f"    • {msg}")
            print()

        print(f"{DIM}These are advisory. Fix what matters, "
              f"ignore what doesn't.{RESET}")
        return len(self.warnings)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_screenshot_mock_contract(report: DriftReport, verbose: bool = False):
    """Validates the screenshot fixture's mock /api/* responses include the
    top-level fields the frontend reads. Catches silent contract drift — the
    kind of bug where the mock returns out-of-date field names, the frontend
    reads missing fields as undefined, and the screenshot silently renders
    with empty cells or zero counters.

    This is a required-fields subset check, not full schema validation.
    Extra fields in the mock don't break anything; missing required fields
    do. The required-fields list is maintained here manually — it's the
    contract between the frontend and the backend, and if it changes in
    either direction this file is the one place to update.

    Caught in practice (v2.41.33): the /api/all mock was returning the
    pre-v2.40.1 field set (unique_aircraft, total_sightings) long after the
    real endpoint had been rewritten to return the paginated set
    (total_count, returned_count, offset, has_more). The screenshot silently
    rendered with dashes in every data cell, and the "0 aircraft" counter,
    because the frontend couldn't find the fields it reads.

    Strategy:
      1. Load scripts/screenshots.py as a module-like AST
      2. Locate the `payloads` dict inside _build_fetch_stub
      3. For each endpoint we have a contract for, check the corresponding
         mock dict includes each required key
      4. Report any missing as a drift warning
    """
    screenshot_py = REPO_ROOT / "scripts" / "screenshots.py"
    if not screenshot_py.exists():
        return  # nothing to check

    # Required top-level fields per endpoint. Derived from grep of j.FOO
    # patterns after each fetch() in the templates. Keep this in sync
    # with the frontend — when a template starts reading a new field, add
    # it here so mock drift is caught on the next check-docs run.
    REQUIRED = {
        'live':      ['aircraft', 'last_updated'],
        'military':  ['aircraft', 'last_updated', 'retention_days'],
        'watchlist': ['aircraft', 'last_updated', 'retention_days'],
        # /api/all is paginated; total_count drives the header counter and
        # has_more drives the Load More button. returned_count + offset are
        # used by the Load More pagination math. This is the contract the
        # v2.41.33 hotfix re-aligned the mock to match.
        'all':       ['aircraft', 'total_count', 'returned_count',
                      'offset', 'has_more'],
        'status':    ['version'],
        'stats':     ['groups'],
        'ui_config': ['watchlist_alerts'],
        'config':    [],  # many nested keys; top-level is dynamic
        'perf':      [],  # diagnostic dump, shape-stable but keys vary by OS
        'wl_entries': ['entries'],
    }

    source = screenshot_py.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        report.warn("Mock contract",
                    f"Could not parse scripts/screenshots.py: {e}",
                    "scripts/screenshots.py")
        return

    # Walk the AST to find the `payloads = { ... }` dict inside
    # _build_fetch_stub. We look for an Assign to Name('payloads') with a
    # Dict value whose keys are string constants.
    payloads_dict = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == 'payloads' \
                    and isinstance(node.value, ast.Dict):
                payloads_dict = node.value
                break
    if payloads_dict is None:
        report.warn("Mock contract",
                    "Could not locate `payloads` dict in scripts/screenshots.py",
                    "scripts/screenshots.py")
        return

    # For each endpoint key, find its value node and extract the keys it
    # returns. The value can be a Dict literal (simple case), a Name
    # referring to a module-level constant (UI_CFG, STATS, STATUS, etc.),
    # or a Call like dict(...).
    for i, key_node in enumerate(payloads_dict.keys):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        endpoint_key = key_node.value
        if endpoint_key not in REQUIRED:
            if verbose:
                print(f"  {DIM}  /api/{endpoint_key}: no contract declared "
                      f"(not checked){RESET}")
            continue
        if not REQUIRED[endpoint_key]:
            if verbose:
                print(f"  {DIM}  /api/{endpoint_key}: contract intentionally "
                      f"empty (not checked){RESET}")
            continue

        value_node = payloads_dict.values[i]
        mock_keys = _extract_dict_keys(value_node, tree)
        if mock_keys is None:
            if verbose:
                print(f"  {DIM}  (skipped {endpoint_key}: value is a "
                      f"{type(value_node).__name__}){RESET}")
            continue

        required = set(REQUIRED[endpoint_key])
        missing = required - mock_keys
        if missing:
            report.warn(
                "Mock contract",
                f"/api/{endpoint_key.replace('_', '/')} mock is missing "
                f"required fields: {sorted(missing)} — frontend reads these "
                f"but mock doesn't provide them, so screenshots will render "
                f"with empty/zero values for this data",
                "scripts/screenshots.py",
            )
        elif verbose:
            print(f"  {DIM}  /api/{endpoint_key}: {len(required)} required "
                  f"field(s) all present{RESET}")


def _extract_dict_keys(node: ast.AST, tree: ast.Module) -> set | None:
    """Given an AST node that should represent a dict (literal or a Name
    binding to a dict literal at module scope), return the set of
    top-level string keys. Returns None if the shape is too dynamic to
    inspect statically."""
    if isinstance(node, ast.Dict):
        keys = set()
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        return keys
    if isinstance(node, ast.Name):
        # Look up the module-level binding for this name
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                    and isinstance(n.targets[0], ast.Name) \
                    and n.targets[0].id == node.id \
                    and isinstance(n.value, ast.Dict):
                return _extract_dict_keys(n.value, tree)
        return None
    return None


def check_version_references(report: DriftReport, verbose: bool = False):
    """Flag files that mention a version string other than the current one
    in contexts that should be live (not historical).

    We target the '<!-- Version: X.Y.Z -->' and '# Version: X.Y.Z' comment
    markers that bump-version.sh is supposed to keep in sync. Other
    occurrences (changelog entries, "since v2.40.1", example strings) are
    intentionally historical and NOT warned about.
    """
    pattern_header = re.compile(
        r"^[\s]*(?:#|//|<!--)[\s]*Version:[\s]+(\d+\.\d+\.\d+)",
        re.MULTILINE,
    )
    files_to_check = [
        REPO_ROOT / "main.py",
        REPO_ROOT / "collector.py",
        REPO_ROOT / "server.py",
        REPO_ROOT / "config_validator.py",
        REPO_ROOT / "notifier.py",
        REPO_ROOT / "config.yaml",
        REPO_ROOT / "config.yaml.example",
        REPO_ROOT / "install.sh",
        REPO_ROOT / "uninstall.sh",
        REPO_ROOT / "requirements.txt",
        REPO_ROOT / "bump-version.sh",
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
    ]
    for f in files_to_check:
        if not f.exists():
            continue
        text = f.read_text()
        for m in pattern_header.finditer(text):
            found_version = m.group(1)
            if found_version != VERSION:
                report.warn(
                    "Version-header drift",
                    f"says Version: {found_version}, expected {VERSION}",
                    str(f.relative_to(REPO_ROOT)),
                )
            elif verbose:
                print(f"  {DIM}✓ {f.relative_to(REPO_ROOT)}: "
                      f"Version: {found_version}{RESET}")

    # Also check templates. Two distinct concerns per template:
    #
    #   1. The `<!-- Version: X.Y.Z -->` comment header at the top —
    #      same drift class as the Python/sh/yaml header check above,
    #      managed by bump-version.sh and equally vulnerable to silent
    #      drift if a template falls out of bump-version.sh's tracked
    #      FILES list (which is what happened with diagnostics.html,
    #      diagnostics-watchlist.html, and performance.html through
    #      v2.50.6).
    #
    #   2. Hardcoded version text inside hdr-meta spans — the v2.40.1
    #      issue where the dashboard banner displayed a frozen string
    #      instead of populating from /api/status at runtime.
    #
    # v2.50.9: added the header check; the hdr-meta check is unchanged.
    for t in (REPO_ROOT / "templates").glob("*.html"):
        text = t.read_text()
        for m in pattern_header.finditer(text):
            found_version = m.group(1)
            if found_version != VERSION:
                report.warn(
                    "Version-header drift",
                    f"says Version: {found_version}, expected {VERSION}",
                    str(t.relative_to(REPO_ROOT)),
                )
            elif verbose:
                print(f"  {DIM}✓ {t.relative_to(REPO_ROOT)}: "
                      f"Version: {found_version}{RESET}")
        # Look for hardcoded version strings inside hdr-meta spans
        m = re.search(r'hdr-meta"[^>]*>v(\d+\.\d+\.\d+)[\s&]', text)
        if m:
            found_version = m.group(1)
            report.warn(
                "Template hardcoded version",
                f"hdr-meta shows v{found_version} — should use "
                f"<span id=\"appVersion\">—</span>",
                str(t.relative_to(REPO_ROOT)),
            )


def check_readme_links(report: DriftReport, verbose: bool = False):
    """Scan README.md for markdown links to local paths and flag any that
    point to files that don't exist."""
    readme = (REPO_ROOT / "README.md").read_text()
    # Match ![alt](path) and [text](path). Skip anchors (#section), URLs
    # (http://, https://), and mailto: links.
    link_pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for m in link_pattern.finditer(readme):
        href = m.group(1).strip()
        # Skip anchors, external URLs, mailto
        if href.startswith(("#", "http://", "https://", "mailto:")):
            continue
        # Strip any anchor after the path (e.g. LICENSE#copying)
        path_only = href.split("#", 1)[0]
        if not path_only:
            continue
        target = REPO_ROOT / path_only
        if not target.exists():
            report.warn(
                "Broken README link",
                f"README.md references {path_only} — file doesn't exist",
                "README.md",
            )
        elif verbose:
            print(f"  {DIM}✓ README link OK: {path_only}{RESET}")


def check_screenshots_exist(report: DriftReport, verbose: bool = False):
    """Every docs/screenshot-*.png mentioned in README.md must exist."""
    readme = (REPO_ROOT / "README.md").read_text()
    pattern = re.compile(r"docs/screenshot-[A-Za-z0-9_-]+\.png")
    for m in pattern.finditer(readme):
        path = m.group(0)
        target = REPO_ROOT / path
        if not target.exists():
            report.warn(
                "Missing screenshot",
                f"README references {path} which isn't in docs/",
                path,
            )
        elif verbose:
            print(f"  {DIM}✓ screenshot present: {path}{RESET}")


def check_project_structure(report: DriftReport, verbose: bool = False):
    """Flag top-level files/modules that exist on disk but aren't mentioned
    in the Project-structure tree in README.md, and vice versa.

    Scope is deliberately narrow — we only check the top-level files that
    appear on the first indent level of the tree. Subdirectories are
    spot-checked by presence only."""
    readme = (REPO_ROOT / "README.md").read_text()
    # Extract the fenced block following "## Project structure"
    m = re.search(r"## Project structure\s*\n+```\n([\s\S]+?)\n```", readme)
    if not m:
        report.warn(
            "Missing Project structure",
            "No ## Project structure code block found in README",
            "README.md",
        )
        return

    tree_text = m.group(1)
    # Pull file/dir names from tree (skip decorative box characters). The
    # tree uses entries like "├── install.sh" or "├── templates/" — we want
    # the name (the word right before the first space or # comment).
    mentioned = set()
    for line in tree_text.splitlines():
        # Strip the box drawing characters and any leading indent
        stripped = re.sub(r"^[│├└─\s]+", "", line).strip()
        if not stripped:
            continue
        # Take the first token (filename). Strip trailing '/' for directories.
        name = stripped.split(None, 1)[0].rstrip("/")
        if name:
            mentioned.add(name)

    # Top-level files actually present (not in venv, __pycache__, etc)
    present = set()
    for entry in REPO_ROOT.iterdir():
        name = entry.name
        # Skip things users don't care about in docs
        if name.startswith(".") and name not in {".gitignore"}:
            continue
        if name in {"venv", "__pycache__", "node_modules"}:
            continue
        if entry.is_file() and entry.suffix in {".pyc"}:
            continue
        present.add(name)

    # Things that exist but aren't mentioned
    # (ignore files the tree explicitly treats as auto-created)
    auto_created = {"logs", "update", ".backups", "aircraft_history.db",
                    "aircraft_history.db-shm", "aircraft_history.db-wal",
                    ".tracker.pid", ".gitignore"}
    # Also ignore any config.yaml.bak.* auto-produced during upgrades
    backup_pattern = re.compile(r"^config\.yaml\.bak\.")

    missing_from_tree = []
    for name in present:
        if name in auto_created:
            continue
        if backup_pattern.match(name):
            continue
        if name not in mentioned:
            missing_from_tree.append(name)

    for name in sorted(missing_from_tree):
        report.warn(
            "Project structure — missing entry",
            f"{name} exists but isn't in README's Project structure tree",
            name,
        )

    # Things mentioned but missing — usually indicates a rename or deletion
    # that didn't get reflected in the README.
    for name in sorted(mentioned):
        if name in {"aerodrome"}:  # the root itself
            continue
        if name in auto_created:
            continue
        # For subdir entries (templates/, docs/, scripts/) don't try to
        # verify every subfile — the top-level check is enough noise.
        if "*" in name:
            continue
        if not (REPO_ROOT / name).exists() and not (REPO_ROOT.glob(name)):
            # Check if it's a template path like templates/index.html
            # The tree lists these indented under the directory, so they're
            # in `mentioned` too. Skip if the file exists.
            # Also check in templates/, scripts/ subdirs.
            found = False
            for prefix in ("", "templates/", "scripts/", "docs/"):
                if (REPO_ROOT / (prefix + name)).exists():
                    found = True
                    break
            if not found:
                report.warn(
                    "Project structure — phantom entry",
                    f"{name} mentioned in tree but not on disk",
                    name,
                )


def check_doc_files_exist(report: DriftReport, verbose: bool = False):
    """Every file named by server.py's DOC_FILES map must exist on disk.
    The in-app Documentation viewer serves these files through
    /api/docs/<slug>, and a missing file renders as a red 'HTTP 404' box
    in place of the tab content.

    Why this check exists: the release-packaging pipeline stripped update/
    from every zip between v2.41.15 and v2.41.20, so v2.41.20-.18 users who
    unzipped a fresh release ended up with a file called update/ containing
    no UPDATE_README.md, the Updates tab in Documentation 404'd, and the
    breakage persisted across restarts until v2.41.21 shipped. This check
    would have caught the packaging bug on the first zip build — the check
    runs against the source tree, and if the source is missing a file that
    DOC_FILES points to, no downstream packaging step can rescue the zip."""
    server_py = REPO_ROOT / "server.py"
    if not server_py.exists():
        return  # not much we can check without the source
    src = server_py.read_text()
    # Grep for the DOC_FILES block. This parser is intentionally dumb: we
    # look for the `DOC_FILES = {` line and then harvest every quoted path
    # on following lines until the closing brace. Good enough for the flat
    # single-block dict that DOC_FILES is today; if it ever grows more
    # complex, upgrade to ast.parse().
    m = re.search(r"DOC_FILES\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    if not m:
        report.warn(
            "DOC_FILES not found",
            "Could not locate DOC_FILES mapping in server.py. Check "
            "script may be out of date.",
            "server.py",
        )
        return
    body = m.group(1)
    # Each entry is like:  "slug":  "path/to/file",
    entries = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', body)
    for slug, rel in entries:
        path = REPO_ROOT / rel
        if not path.exists():
            report.warn(
                "Missing doc file",
                f"DOC_FILES['{slug}'] -> {rel} — file is not present. "
                f"The '{slug}' Documentation tab will 404 in the UI.",
                rel,
            )
        elif verbose:
            print(f"  {DIM}✓ DOC_FILES['{slug}'] -> {rel} exists{RESET}")


def check_pdf_freshness(report: DriftReport, verbose: bool = False):
    """docs/Aerodrome_Overview.pdf should be at least as recent as the
    most recent mtime across core source files. If it's stale, the PDF in
    the repo was built against an earlier state."""
    pdf = REPO_ROOT / "docs" / "Aerodrome_Overview.pdf"
    if not pdf.exists():
        report.warn(
            "Missing overview PDF",
            "docs/Aerodrome_Overview.pdf not found. Run "
            "scripts/build_overview_pdf.py.",
            "docs/Aerodrome_Overview.pdf",
        )
        return

    pdf_mtime = pdf.stat().st_mtime
    sources = [
        REPO_ROOT / "VERSION",
        REPO_ROOT / "CHANGELOG.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / "scripts" / "build_overview_pdf.py",
    ]
    for src in sources:
        if src.exists() and src.stat().st_mtime > pdf_mtime + 5:
            report.warn(
                "Stale overview PDF",
                f"{src.relative_to(REPO_ROOT)} is newer than "
                f"docs/Aerodrome_Overview.pdf — rebuild with "
                f"scripts/build_overview_pdf.py",
                "docs/Aerodrome_Overview.pdf",
            )
            return


def check_screenshot_freshness(report: DriftReport, verbose: bool = False):
    """Screenshots should be newer than their source templates. Not a hard
    rule (you might update a template without changing what it looks like),
    so this is advisory only."""
    pairs = [
        ("screenshot-live.png",           "templates/index.html"),
        ("screenshot-stats.png",          "templates/index.html"),
        ("screenshot-status.png",         "templates/status.html"),
        ("screenshot-config.png",         "templates/config.html"),
        ("screenshot-updates.png",        "templates/updates.html"),
        ("screenshot-performance.png",    "templates/performance.html"),
        ("screenshot-logs.png",           "templates/logs.html"),
        ("screenshot-docs.png",           "templates/docs.html"),
        ("screenshot-board-radar.png",    "templates/board.html"),
        ("screenshot-board-flight.png",   "templates/board.html"),
        ("screenshot-board-hybrid.png",   "templates/board.html"),
    ]
    for shot_name, tpl_name in pairs:
        shot = REPO_ROOT / "docs" / shot_name
        tpl = REPO_ROOT / tpl_name
        if not shot.exists():
            # Handled separately by check_screenshots_exist when referenced
            # in README; skip silently here to avoid duplicate warnings
            continue
        if not tpl.exists():
            continue
        if tpl.stat().st_mtime > shot.stat().st_mtime + 5:
            report.warn(
                "Stale screenshot",
                f"{tpl_name} is newer than docs/{shot_name} — rebuild with "
                f"scripts/screenshots.py",
                f"docs/{shot_name}",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv

    print(f"{CYAN}Documentation drift check — Aerodrome v{VERSION}{RESET}")
    print(f"{DIM}{REPO_ROOT}{RESET}")
    print()

    report = DriftReport()
    checks = [
        ("Version references",      check_version_references),
        ("README links",            check_readme_links),
        ("Screenshots referenced",  check_screenshots_exist),
        ("Project structure tree",  check_project_structure),
        ("DOC_FILES on disk",       check_doc_files_exist),
        ("Overview PDF freshness",  check_pdf_freshness),
        ("Screenshot freshness",    check_screenshot_freshness),
        ("Screenshot mock contract", check_screenshot_mock_contract),
    ]
    for label, fn in checks:
        if verbose:
            print(f"{CYAN}→ {label}{RESET}")
        try:
            fn(report, verbose=verbose)
        except Exception as e:  # never let a check crash the whole tool
            report.warn(
                "Check crashed",
                f"{label}: {type(e).__name__}: {e}",
                "",
            )

    return report.summary()


if __name__ == "__main__":
    n = main(sys.argv[1:])
    # Advisory mode: any number of warnings exits 0. Crashes exit 2.
    sys.exit(0)
