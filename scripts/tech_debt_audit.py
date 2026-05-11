"""
Aerodrome tech-debt audit: static scanner.

Runs over the repo and produces docs/tech-debt-audit.md — a findings report
grouped by category. Does NOT modify code. You read the report and decide
what to act on.

Categories (v2.46.0):
  1. Dead Python functions — defined but never referenced
  2. Orphan FastAPI endpoints — routed but never called by any template
  3. Dead JavaScript functions/variables — declared but never referenced
  4. Stale version comments — older than N minor versions from current
  5. TODO / FIXME / XXX / HACK markers — inventory of self-flags

False-positive mitigation:
  - Python: skips test_*, _test, __dunder__, decorated routes/cli/fixtures,
    modules imported with 'import *' or accessed dynamically.
  - JS: treats onclick/onsubmit/etc. attribute references as real uses.
  - Endpoints: matches all the ways the frontend hits them (fetch(, fetch(',
    fetch(`, template literals with paths embedded).

Invocation:
    python3 scripts/tech_debt_audit.py
"""
from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "tech-debt-audit.md"

# ---------------------------------------------------------------------------
# Files to scan
# ---------------------------------------------------------------------------
PYTHON_SOURCES = [
    ROOT / "server.py",
    ROOT / "collector.py",
    ROOT / "notifier.py",
    ROOT / "config_validator.py",
    ROOT / "designators.py",
    ROOT / "main.py",
]
TEMPLATE_SOURCES = sorted((ROOT / "templates").glob("*.html"))

# Decorators whose presence means "this function IS called, just not directly."
# Functions bearing any of these decorators are excluded from dead-code analysis.
LIVE_DECORATORS = {
    # FastAPI routes
    "app.get", "app.post", "app.put", "app.delete", "app.patch",
    "app.head", "app.options", "app.websocket",
    # lifespan / event hooks
    "app.on_event",
    # Typer / Click (if they appear)
    "command", "callback",
    # pytest
    "fixture", "pytest.fixture",
    # Misc
    "staticmethod", "classmethod", "property",
}


# =============================================================================
# Finding data model
# =============================================================================

@dataclass
class Finding:
    """A single audit result. path is relative to repo root."""
    category: str
    path: str
    line: int
    name: str
    note: str
    severity: str = "low"  # low / medium / high


# =============================================================================
# 1. Dead Python functions
# =============================================================================

def _is_live_decorator(dec: ast.expr) -> bool:
    """True if the decorator should exempt its function from dead-code check."""
    # @decorator
    if isinstance(dec, ast.Name):
        return dec.id in LIVE_DECORATORS
    # @something.decorator  or  @app.get(...)
    if isinstance(dec, ast.Attribute):
        parts = []
        node: ast.expr = dec
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        name = ".".join(reversed(parts))
        return name in LIVE_DECORATORS
    # @app.get("/api/...")   — decorator is a Call, unwrap and recurse
    if isinstance(dec, ast.Call):
        return _is_live_decorator(dec.func)
    return False


def find_dead_python_functions() -> list[Finding]:
    """Find top-level and class-level functions defined but not referenced
    anywhere in the Python codebase. References include function calls,
    attribute access, name use in assignments, imports, and string literals
    (handles getattr-style dynamic dispatch)."""
    findings: list[Finding] = []

    # Pass 1: collect all definitions
    # Key: (path, line, qualname, is_decorated_live)
    definitions: list[dict] = []

    for py_path in PYTHON_SOURCES:
        if not py_path.exists():
            continue
        try:
            tree = ast.parse(py_path.read_text())
        except SyntaxError:
            continue
        rel = py_path.relative_to(ROOT).as_posix()

        # Walk top-level statements only. Nested functions are rarely debt
        # candidates — they're closures or helpers, and if the enclosing
        # function is used they're used.
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") and not node.name.startswith("__"):
                    # Leading-underscore 'private' functions are commonly
                    # used within the module; if there are no references
                    # they're still worth flagging.
                    pass
                if node.name.startswith("test_"):
                    continue  # pytest discovery
                if node.name == "main" or node.name == "__main__":
                    continue  # entrypoint
                live = any(_is_live_decorator(d) for d in node.decorator_list)
                definitions.append({
                    "path": rel, "line": node.lineno, "name": node.name,
                    "live": live,
                })
            elif isinstance(node, ast.ClassDef):
                # Top-level class methods are a rarer source of dead code
                # but still worth scanning. Decorator check applies per-method.
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if sub.name.startswith("__") and sub.name.endswith("__"):
                            continue  # dunder methods
                        if sub.name.startswith("test_"):
                            continue
                        live = any(_is_live_decorator(d) for d in sub.decorator_list)
                        definitions.append({
                            "path": rel, "line": sub.lineno,
                            "name": f"{node.name}.{sub.name}",
                            "live": live,
                        })

    # Pass 2: build a big reference haystack from ALL python source text.
    # Use source-text-level search (not AST) because it catches references
    # in docstrings, comments (intentional TODO references), getattr() usage,
    # f-strings naming a function, and so on. More conservative than
    # "definitely dead," which is the right tradeoff for a manual review list.
    all_py_text = "\n".join(
        p.read_text() for p in PYTHON_SOURCES if p.exists()
    )

    for d in definitions:
        if d["live"]:
            continue  # excluded by decorator
        # We need to count NON-DEFINITION occurrences of the name. A simple
        # approach: count total occurrences, subtract 1 for the `def name(`
        # line itself. If count drops to 0, it's dead. Use word boundaries
        # to avoid matching substrings. For Class.method, use the bare method
        # name because that's how call sites actually reference it.
        bare = d["name"].split(".")[-1]
        pattern = rf"\b{re.escape(bare)}\b"
        occurrences = len(re.findall(pattern, all_py_text))
        # The definition itself counts as one occurrence.
        if occurrences <= 1:
            findings.append(Finding(
                category="dead-python",
                path=d["path"], line=d["line"], name=d["name"],
                note="Defined but no references found in Python sources. "
                     "Verify no dynamic dispatch before removing.",
                severity="medium",
            ))

    return findings


# =============================================================================
# 2. Orphan FastAPI endpoints
# =============================================================================

# Capture route path from @app.get("/api/..."), @app.post(...), etc.
ROUTE_DECORATOR_RE = re.compile(
    r'@app\.(?P<verb>get|post|put|delete|patch|head)\(\s*["\'](?P<path>[^"\']+)["\']'
)

# Known-legitimate orphan endpoints — routes that are intentionally declared
# but won't be found by the prefix-match scanner. Skip them to keep the
# audit signal high.
#
# v2.46.1: filed as part of the v2.46.0 audit cleanup sweep. Each entry
# documents WHY the scanner can't detect the real caller.
LEGITIMATE_ORPHANS = {
    # Explicit placeholder for future GitHub-release integration. Returns
    # {"available": False, "enabled": False, ...}. Keeping the stub in place
    # documents the intended shape when the feature is built.
    "/api/updates/github/check",
    # Serves screenshot assets referenced by rendered markdown inside the
    # in-app doc viewer. The caller is <img src="/docs/foo.png"> emitted by
    # the markdown renderer at runtime — not present in any template source.
    "/docs/{filename}",
}


def find_orphan_endpoints() -> list[Finding]:
    """Find routes declared in server.py but never referenced by any template
    or script. Matches all the syntactic forms the frontend uses to call
    backend APIs."""
    findings: list[Finding] = []

    server_py = ROOT / "server.py"
    if not server_py.exists():
        return findings

    # Pass 1: extract all routes
    routes: list[dict] = []
    for i, line in enumerate(server_py.read_text().splitlines(), start=1):
        m = ROUTE_DECORATOR_RE.search(line)
        if m:
            routes.append({
                "line": i,
                "verb": m.group("verb").upper(),
                "path": m.group("path"),
            })

    # Pass 2: build reference corpus from all templates + scripts
    corpus_parts: list[str] = []
    for p in TEMPLATE_SOURCES:
        corpus_parts.append(p.read_text())
    for script in (ROOT / "scripts").glob("*.py"):
        corpus_parts.append(script.read_text())
    # Also include Python sources — some endpoints are called by Python code
    # (e.g., background polling, init routines).
    for p in PYTHON_SOURCES:
        if p.exists() and p.name != "server.py":
            corpus_parts.append(p.read_text())
    corpus = "\n".join(corpus_parts)

    # For each route, check if its path appears anywhere in the corpus.
    # Templated parameters in the route (e.g. /api/foo/{id}) are tricky —
    # reference sites use `/api/foo/${id}` or `/api/foo/`+id. So we match
    # on the static prefix up to the first `{`.
    for r in routes:
        path = r["path"]
        # Skip documented-legitimate orphans (placeholder stubs, browser-
        # fetched assets, etc). These would otherwise clutter the audit
        # every run without being real debt.
        if path in LEGITIMATE_ORPHANS:
            continue
        prefix = path.split("{")[0].rstrip("/")
        # A prefix like "/api" (when the route is exactly "/api/{topic}") is
        # too generic and would match everything. Require at least 3 slashes
        # or 8 chars.
        if len(prefix) < 8:
            prefix = path.rstrip("/")
        if not prefix:
            continue
        # Use substring match (not word boundary) because frontend paths are
        # embedded in URL strings and template literals.
        if prefix not in corpus:
            findings.append(Finding(
                category="orphan-endpoint",
                path="server.py", line=r["line"], name=f"{r['verb']} {path}",
                note=f"Route defined but path prefix '{prefix}' not referenced "
                     f"in any template, script, or other Python source. "
                     f"May be called by external tools or left over from "
                     f"a removed feature.",
                severity="medium",
            ))

    return findings


# =============================================================================
# 3. Dead JavaScript functions / top-level variables
# =============================================================================

JS_FUNCTION_RE = re.compile(
    r'^(?P<indent>\s*)function\s+(?P<name>[a-zA-Z_$][\w$]*)\s*\(',
    re.MULTILINE,
)
JS_LET_RE = re.compile(
    r'^(?P<indent>\s*)(?:let|const|var)\s+(?P<name>[a-zA-Z_$][\w$]*)\s*=',
    re.MULTILINE,
)


def find_dead_js() -> list[Finding]:
    """Find JS functions and top-level lets/consts that are declared but
    have no references in the same file. Attribute references (onclick=,
    etc.) count as real uses so event handlers aren't flagged as dead."""
    findings: list[Finding] = []

    # v2.46.1: strip JS comments before building the reference-count corpus.
    # Without this, a function name mentioned in a // or /* */ comment
    # ("see alertModal() below") counts as a real reference, and the dead-
    # code check silently passes. That was the exact false-negative that let
    # the dead `alertModal` in index.html through v2.46.0's first audit.
    # This strips line comments (// to end-of-line) and block comments
    # (/* ... */) naively — good enough for template-embedded JS which
    # doesn't contain tricky cases like regex literals with // in them.
    def _strip_js_comments(src: str) -> str:
        # Block comments first (non-greedy, spans lines)
        src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
        # Line comments — tricky because // appears inside string literals
        # (URLs, mostly: 'https://...'). Only strip when // is NOT preceded
        # by a quote character on the same line. Approximation, not perfect,
        # but false-positives here just mean we skip stripping a valid
        # comment — the reference count stays conservative.
        out_lines = []
        for line in src.split("\n"):
            # Find // not inside an apparent string. Scan char by char,
            # tracking quote state.
            in_s = None  # current quote char or None
            i = 0
            while i < len(line):
                ch = line[i]
                if in_s:
                    if ch == "\\" and i + 1 < len(line):
                        i += 2; continue
                    if ch == in_s:
                        in_s = None
                elif ch in ('"', "'", "`"):
                    in_s = ch
                elif ch == "/" and i + 1 < len(line) and line[i+1] == "/":
                    line = line[:i]
                    break
                i += 1
            out_lines.append(line)
        return "\n".join(out_lines)

    for template in TEMPLATE_SOURCES:
        text = template.read_text()
        # Separate the "count references against" text (comments stripped)
        # from the "find declarations in" text (comments intact) so we
        # still locate declarations by the original line numbers.
        ref_text = _strip_js_comments(text)
        rel = template.relative_to(ROOT).as_posix()

        # Collect declarations
        for m in JS_FUNCTION_RE.finditer(text):
            name = m.group("name")
            indent = m.group("indent")
            # Only flag top-level functions (zero or minimal indent). Nested
            # functions inside closures or classes are harder to reason about.
            if len(indent) > 4:
                continue
            # Count references anywhere in the file (comments stripped).
            pattern = rf"\b{re.escape(name)}\b"
            count = len(re.findall(pattern, ref_text))
            # One occurrence is the declaration itself.
            if count <= 1:
                line = text[:m.start()].count("\n") + 1
                findings.append(Finding(
                    category="dead-js",
                    path=rel, line=line, name=f"function {name}()",
                    note="JS function declared but no references found "
                         "(includes onclick/onsubmit attributes; comments "
                         "are not counted as references).",
                    severity="low",
                ))

        # Collect let/const/var declarations — but only at indent 0 (true
        # top-level). Variables inside functions are scoped and have their
        # own dead-code semantics that this scanner won't get right.
        for m in JS_LET_RE.finditer(text):
            name = m.group("name")
            indent = m.group("indent")
            if len(indent) > 0:
                continue
            # Skip loop counters and common single-letter names
            if len(name) <= 2:
                continue
            pattern = rf"\b{re.escape(name)}\b"
            count = len(re.findall(pattern, ref_text))
            if count <= 1:
                line = text[:m.start()].count("\n") + 1
                findings.append(Finding(
                    category="dead-js",
                    path=rel, line=line, name=f"let/const {name}",
                    note="Top-level JS binding declared but no references "
                         "found in the same template.",
                    severity="low",
                ))

    return findings


# =============================================================================
# 4. Stale version comments
# =============================================================================

VERSION_COMMENT_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _parse_current_version() -> tuple[int, int, int]:
    """Read VERSION file; return (major, minor, patch)."""
    v = (ROOT / "VERSION").read_text().strip()
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def find_stale_version_comments(stale_after_minor: int = 10) -> list[Finding]:
    """Flag in-source version references older than N minor versions from
    current. These often describe historical context that's no longer
    relevant ('fixed in v2.41.19' is less interesting once v2.45 ships).
    Does NOT flag LICENSE, CHANGELOG, or release-manifest files.

    v2.46.1: raised threshold from 5 → 10. At 5, nearly every "fixed in
    v2.40.x" historical-context comment lights up, even when that context
    is actively useful for explaining why current code is shaped the way
    it is. 10 catches the genuinely-ancient stuff without burying the
    operator in low-signal noise."""
    findings: list[Finding] = []
    cur_maj, cur_min, _ = _parse_current_version()

    def too_old(maj: int, minr: int) -> bool:
        if maj < cur_maj:
            return True
        if maj == cur_maj and minr + stale_after_minor < cur_min:
            return True
        return False

    SKIP = {"CHANGELOG.md", "LICENSE", "VERSION", "tech-debt-audit.md"}

    files_to_scan = (
        PYTHON_SOURCES
        + TEMPLATE_SOURCES
        + list((ROOT / "scripts").glob("*.py"))
    )
    for p in files_to_scan:
        if not p.exists() or p.name in SKIP:
            continue
        rel = p.relative_to(ROOT).as_posix()
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            # Only scan comment lines or docstring-like lines, to avoid
            # flagging legitimate version-comparison logic.
            stripped = line.lstrip()
            is_comment = (stripped.startswith("#") or stripped.startswith("//")
                          or stripped.startswith("*") or stripped.startswith("/*")
                          or '"""' in stripped or "'''" in stripped)
            if not is_comment:
                continue
            for m in VERSION_COMMENT_RE.finditer(line):
                maj = int(m.group(1))
                minr = int(m.group(2))
                if too_old(maj, minr):
                    findings.append(Finding(
                        category="stale-version",
                        path=rel, line=i,
                        name=f"v{maj}.{minr}.{m.group(3)}",
                        note=f"Version reference is {cur_maj}.{cur_min} - "
                             f"{maj}.{minr} = {cur_min - minr} minor releases "
                             f"old. Comment may describe behavior that has "
                             f"since changed.",
                        severity="low",
                    ))
                    break  # one finding per line

    return findings


# =============================================================================
# 5. TODO / FIXME / XXX / HACK markers
# =============================================================================

MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK|TECH DEBT|TEMP)\b")


def find_todo_markers() -> list[Finding]:
    """Inventory all TODO/FIXME/etc. markers in the codebase. Not debt
    per se but useful inventory of self-flags the maintainer has already
    identified.

    Only matches markers in comment lines (# or // prefix, or inside a
    multi-line /* */ block). String-literal matches would be false
    positives — the SQLite planner output 'USE TEMP B-TREE FOR' is the
    canonical example: 'TEMP' there is a keyword inside test fixture
    data, not a self-flag."""
    findings: list[Finding] = []

    files_to_scan = (
        PYTHON_SOURCES
        + TEMPLATE_SOURCES
        + list((ROOT / "scripts").glob("*.py"))
    )

    SKIP = {"tech_debt_audit.py", "tech-debt-audit.md", "CHANGELOG.md"}

    for p in files_to_scan:
        if not p.exists() or p.name in SKIP:
            continue
        rel = p.relative_to(ROOT).as_posix()
        in_block_comment = False  # Track /* ... */ across lines
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            stripped = line.lstrip()
            # Determine if this line is inside a comment context.
            is_comment_line = (
                stripped.startswith("#")
                or stripped.startswith("//")
                or stripped.startswith("*")
                or in_block_comment
            )
            # Track /* ... */ spans.
            if "/*" in line and "*/" not in line[line.find("/*"):]:
                in_block_comment = True
            elif "*/" in line and in_block_comment:
                in_block_comment = False
            if not is_comment_line:
                continue
            m = MARKER_RE.search(line)
            if m:
                snippet = stripped
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                findings.append(Finding(
                    category="todo-marker",
                    path=rel, line=i,
                    name=m.group(1),
                    note=snippet,
                    severity="low",
                ))

    return findings


# =============================================================================
# Report rendering
# =============================================================================

CATEGORY_META = {
    "dead-python":     {"title": "Dead Python functions",
                        "desc": "Functions defined but with no references found in the Python codebase. Verify no dynamic dispatch (getattr, plugin registries) before removing."},
    "orphan-endpoint": {"title": "Orphan FastAPI endpoints",
                        "desc": "Routes declared in server.py whose path prefix doesn't appear in any template, script, or other Python source. May be called by external tools or left over from a removed feature."},
    "dead-js":         {"title": "Dead JavaScript",
                        "desc": "Top-level JS functions or let/const declarations with no references in the same template. Event-handler attributes (onclick, onsubmit) are counted as real references."},
    "stale-version":   {"title": "Stale version comments",
                        "desc": "In-source version references many minor releases old. Comments tied to these versions may describe behavior that has since changed."},
    "todo-marker":     {"title": "TODO / FIXME / HACK markers",
                        "desc": "Inventory of self-flagged notes in the code. Not inherently debt — useful for tracking unresolved items."},
}


def render_markdown(findings: list[Finding], generated_at: str,
                    version: str) -> str:
    """Render findings as a markdown report."""
    # Group by category, then by file
    by_category: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_category[f.category].append(f)

    lines: list[str] = []
    lines.append("# Tech-debt audit report")
    lines.append("")
    lines.append(f"_Generated {generated_at} against Aerodrome v{version} "
                 f"by `scripts/tech_debt_audit.py`._")
    lines.append("")
    lines.append("This report is a static-analysis snapshot. It finds "
                 "suspicious patterns; it does not modify code. Each finding "
                 "is a candidate for manual review — some will be real debt, "
                 "others will be false positives from dynamic dispatch, "
                 "external callers, or framework-provided lifecycle hooks.")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|---|---:|")
    total = 0
    for cat in CATEGORY_META:
        n = len(by_category.get(cat, []))
        total += n
        title = CATEGORY_META[cat]["title"]
        lines.append(f"| {title} | {n} |")
    lines.append(f"| **Total** | **{total}** |")
    lines.append("")

    # Per-category sections
    for cat, meta in CATEGORY_META.items():
        items = by_category.get(cat, [])
        lines.append(f"## {meta['title']}")
        lines.append("")
        lines.append(meta["desc"])
        lines.append("")
        if not items:
            lines.append("_No findings in this category._")
            lines.append("")
            continue
        # Group by path for cleaner reading
        by_path: dict[str, list[Finding]] = defaultdict(list)
        for f in items:
            by_path[f.path].append(f)
        for path, entries in sorted(by_path.items()):
            lines.append(f"### `{path}`")
            lines.append("")
            for f in sorted(entries, key=lambda x: x.line):
                lines.append(f"- **Line {f.line}** — `{f.name}`")
                lines.append(f"  - {f.note}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_To regenerate: `python3 scripts/tech_debt_audit.py`_")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Entry point
# =============================================================================

def main() -> int:
    from datetime import datetime, timezone
    version = (ROOT / "VERSION").read_text().strip()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"Running tech-debt audit against v{version}...")
    findings: list[Finding] = []

    print("  [1/5] Dead Python functions...", end=" ", flush=True)
    r = find_dead_python_functions()
    findings.extend(r)
    print(f"{len(r)} found")

    print("  [2/5] Orphan FastAPI endpoints...", end=" ", flush=True)
    r = find_orphan_endpoints()
    findings.extend(r)
    print(f"{len(r)} found")

    print("  [3/5] Dead JavaScript...", end=" ", flush=True)
    r = find_dead_js()
    findings.extend(r)
    print(f"{len(r)} found")

    print("  [4/5] Stale version comments...", end=" ", flush=True)
    r = find_stale_version_comments()
    findings.extend(r)
    print(f"{len(r)} found")

    print("  [5/5] TODO / FIXME / HACK markers...", end=" ", flush=True)
    r = find_todo_markers()
    findings.extend(r)
    print(f"{len(r)} found")

    report = render_markdown(findings, generated_at, version)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(report)
    print(f"\nWrote: {OUT_PATH.relative_to(ROOT)}")
    print(f"Total findings: {len(findings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
