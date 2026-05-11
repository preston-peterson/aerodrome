"""
Build the Aerodrome overview PDF.

Generates docs/Aerodrome_Overview.pdf from the current state of the repo:
reads VERSION for version strings, counts releases from CHANGELOG.md, and
tallies code-line counts from the actual source tree. Screenshots are pulled
from docs/screenshot-*.png (produced by scripts/screenshots.py).

Usage, from the repo root:
    python3 scripts/build_overview_pdf.py

Or via bump-version.sh, which calls this automatically after a bump so the
PDF shipped inside every release zip is always current.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image,
    Table, TableStyle, KeepTogether, CondPageBreak,
)
from pathlib import Path
import os
import re
import subprocess

# -----------------------------------------------------------------------------
# Repo layout — all paths are relative to the repo root, which is the parent
# of this script's directory. This lets the PDF builder run from anywhere
# (bump-version.sh, CI, a dev's laptop) without caring about cwd.
# -----------------------------------------------------------------------------
REPO_ROOT       = Path(__file__).resolve().parent.parent
VERSION         = (REPO_ROOT / "VERSION").read_text().strip()
SCREENSHOT_DIR  = REPO_ROOT / "docs"
OUT_PATH        = REPO_ROOT / "docs" / "Aerodrome_Overview.pdf"

# -----------------------------------------------------------------------------
# Palette — match Aerodrome's UI colors so the PDF feels like it belongs to
# the same design system (dark bg, cyan accent, neutral grays). But we print
# on white pages for practical sharing — so a few colors get inverted.
# -----------------------------------------------------------------------------
CYAN    = colors.HexColor("#06b6d4")
INK     = colors.HexColor("#1a2236")
MUTED   = colors.HexColor("#64748b")
RULE    = colors.HexColor("#cbd5e1")
AMBER   = colors.HexColor("#f59e0b")
GREEN   = colors.HexColor("#22c55e")
BG_DARK = colors.HexColor("#0a0e17")  # for the cover page only
CARD_BG = colors.HexColor("#f8fafc")   # light card for stat callouts


# -----------------------------------------------------------------------------
# Repo stats — computed at build time so they can't drift.
# -----------------------------------------------------------------------------

def count_releases() -> int:
    """Number of versioned entries in CHANGELOG.md."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    return len(re.findall(r"^## \[", text, flags=re.MULTILINE))


def count_python_lines() -> int:
    """Total lines across the Python modules we ship (not including venv,
    pycache, or the scripts/ tooling directory)."""
    modules = ["main.py", "server.py", "collector.py", "config_validator.py",
               "notifier.py", "designators.py"]
    total = 0
    for m in modules:
        p = REPO_ROOT / m
        if p.exists():
            total += len(p.read_text().splitlines())
    return total


def count_python_modules() -> int:
    """How many .py modules are being counted by count_python_lines()."""
    modules = ["main.py", "server.py", "collector.py", "config_validator.py",
               "notifier.py", "designators.py"]
    return sum(1 for m in modules if (REPO_ROOT / m).exists())


def count_template_lines() -> int:
    """Total lines across all HTML templates."""
    total = 0
    for p in sorted((REPO_ROOT / "templates").glob("*.html")):
        total += len(p.read_text().splitlines())
    return total


def count_templates() -> int:
    return len(list((REPO_ROOT / "templates").glob("*.html")))


def count_api_endpoints() -> int:
    """Count FastAPI route decorators in server.py."""
    text = (REPO_ROOT / "server.py").read_text()
    return len(re.findall(r"@app\.(get|post|put|delete|patch)\(", text))


def count_sqlite_tables() -> int:
    """Count CREATE TABLE statements in collector.py (schema lives there)."""
    text = (REPO_ROOT / "collector.py").read_text()
    return len(re.findall(r"CREATE TABLE IF NOT EXISTS", text))


# Gather stats once, log them so the bump log shows what went into the doc.
STATS = {
    "releases":       count_releases(),
    "python_lines":   count_python_lines(),
    "python_modules": count_python_modules(),
    "html_lines":     count_template_lines(),
    "html_pages":     count_templates(),
    "endpoints":      count_api_endpoints(),
    "sqlite_tables":  count_sqlite_tables(),
}


# -----------------------------------------------------------------------------
# Styles — defined once, used throughout.
# -----------------------------------------------------------------------------
styles = getSampleStyleSheet()

s_h1 = ParagraphStyle(
    "h1", parent=styles["Heading1"],
    fontName="Helvetica-Bold", fontSize=20, leading=26,
    textColor=INK, alignment=TA_LEFT,
    spaceBefore=0, spaceAfter=6,
)
s_h2 = ParagraphStyle(
    "h2", parent=styles["Heading2"],
    fontName="Helvetica-Bold", fontSize=13, leading=18,
    textColor=CYAN, alignment=TA_LEFT,
    spaceBefore=14, spaceAfter=4,
)
s_body = ParagraphStyle(
    "body", parent=styles["Normal"],
    fontName="Helvetica", fontSize=10, leading=15,
    textColor=INK, alignment=TA_JUSTIFY,
    spaceAfter=8,
)
s_body_left = ParagraphStyle("body_left", parent=s_body, alignment=TA_LEFT)
s_lead = ParagraphStyle("lead", parent=s_body, fontSize=11, leading=17)
s_caption = ParagraphStyle(
    "caption", parent=styles["Italic"],
    fontName="Helvetica-Oblique", fontSize=9, leading=12,
    textColor=MUTED, alignment=TA_CENTER,
    spaceBefore=3, spaceAfter=14,
)
s_stat_number = ParagraphStyle(
    "stat_number", parent=styles["Normal"],
    fontName="Helvetica-Bold", fontSize=22, leading=26,
    textColor=CYAN, alignment=TA_LEFT, spaceAfter=0,
)
s_stat_label = ParagraphStyle(
    "stat_label", parent=styles["Normal"],
    fontName="Helvetica", fontSize=9, leading=12,
    textColor=MUTED, alignment=TA_LEFT, spaceAfter=0,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def fitted_image(path, max_width_in=6.5, max_height_in=7.5):
    """Scale an image to fit within the given bounds while preserving aspect."""
    try:
        import PIL.Image as PILImage
        img = PILImage.open(path)
        w, h = img.size
    except Exception:
        # Fall back to letting reportlab figure out dimensions
        return Image(str(path), width=max_width_in * inch,
                     height=max_height_in * inch)
    aspect = w / h
    max_w_pt = max_width_in * inch
    max_h_pt = max_height_in * inch
    target_w = max_w_pt
    target_h = target_w / aspect
    if target_h > max_h_pt:
        target_h = max_h_pt
        target_w = target_h * aspect
    return Image(str(path), width=target_w, height=target_h)


def stat_card(number, label):
    """Stat card used in the 'fun stats' section.

    v2.44.1: dropped hardcoded rowHeights. Previous values (0.50in + 0.35in)
    were tuned for 2-digit numbers and single-line labels. Bumped to 4-digit
    numbers with a thousands separator ('9,839') and two-line wrapped labels
    overflowed the row boxes and visually collided with the label row below.
    Letting reportlab auto-size the rows fixes the overlap for every card
    size encountered so far, and the BOX + BACKGROUND styles follow the
    auto-computed height correctly."""
    tbl = Table(
        [[Paragraph(str(number), s_stat_number)],
         [Paragraph(label, s_stat_label)]],
        colWidths=[1.95 * inch],
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (0, 0), 10),
        ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (0, 1), 2),
        ("BOTTOMPADDING", (0, 1), (0, 1), 10),
    ]))
    return tbl


def feature_row(title, body):
    return Paragraph(
        f'<b>{title}.</b> <font color="#1a2236">{body}</font>',
        s_body_left,
    )


def _fmt(n):
    """Human-grouped integer: 9712 -> '9,712'."""
    return f"{n:,}"


# -----------------------------------------------------------------------------
# Page decorations
# -----------------------------------------------------------------------------

def draw_cover_page(canv, doc):
    """First page only — dark background, big title."""
    canv.saveState()
    canv.setFillColor(BG_DARK)
    canv.rect(0, 0, letter[0], letter[1], fill=1, stroke=0)

    canv.setFillColor(CYAN)
    canv.rect(0, letter[1] - 6, letter[0], 6, fill=1, stroke=0)

    left = 0.9 * inch
    top = letter[1] - 2.2 * inch

    canv.setFillColor(colors.HexColor("#94a3b8"))
    canv.setFont("Courier", 9)
    canv.drawString(left, top + 60, "ADS-B / AIRCRAFT TRACKING / SELF-HOSTED")

    canv.setFillColor(colors.white)
    canv.setFont("Helvetica-Bold", 64)
    canv.drawString(left, top, "Aerodrome")

    canv.setFillColor(CYAN)
    canv.setFont("Helvetica", 18)
    canv.drawString(left, top - 36, "A clean, modern ADS-B aircraft tracker")
    canv.setFillColor(colors.HexColor("#e2e8f0"))
    canv.setFont("Helvetica", 18)
    canv.drawString(left, top - 60, "for your home receiver.")

    desc_y = 3.8 * inch
    canv.setFillColor(colors.HexColor("#cbd5e1"))
    canv.setFont("Helvetica", 11)
    lines = [
        "Turn your local ADS-B receiver (readsb, dump1090, tar1090) into a",
        "polished web dashboard: live aircraft, a personal watchlist,",
        "auto-detected military flights, a searchable history, and a rich",
        "statistics view — with optional push notifications to your phone.",
    ]
    for i, line in enumerate(lines):
        canv.drawString(left, desc_y - i * 16, line)

    canv.setFillColor(colors.HexColor("#64748b"))
    canv.setFont("Courier", 9)
    canv.drawString(left, 0.8 * inch, f"Version {VERSION}")
    canv.drawRightString(letter[0] - left, 0.8 * inch,
                         "Aerodrome Project Overview")

    canv.setStrokeColor(colors.HexColor("#2a3a54"))
    canv.setLineWidth(0.5)
    canv.line(left, 0.65 * inch, letter[0] - left, 0.65 * inch)

    canv.restoreState()


def draw_interior_page(canv, doc):
    """Every page after the cover — white background, footer."""
    canv.saveState()
    canv.setFillColor(MUTED)
    canv.setFont("Helvetica", 8)
    canv.drawString(0.9 * inch, 0.5 * inch, f"Aerodrome — v{VERSION}")
    canv.drawRightString(letter[0] - 0.9 * inch, 0.5 * inch,
                         f"Page {doc.page - 1}")
    canv.setStrokeColor(RULE)
    canv.setLineWidth(0.3)
    canv.line(0.9 * inch, 0.68 * inch, letter[0] - 0.9 * inch, 0.68 * inch)
    canv.restoreState()


# -----------------------------------------------------------------------------
# Build
# -----------------------------------------------------------------------------

def build():
    doc = SimpleDocTemplate(
        str(OUT_PATH), pagesize=letter,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        title=f"Aerodrome — Project Overview (v{VERSION})",
        author="Aerodrome Project",
        subject="A self-hosted ADS-B aircraft tracker",
    )

    story = []

    # Cover (decorations drawn by the page callback)
    story.append(Spacer(1, 8 * inch))
    story.append(PageBreak())

    # Content from docs/overview.md. The parser below is purpose-built
    # rather than using a full markdown library — keeps the dependency
    # surface small, and the syntax we need (headings, paragraphs,
    # emphasis, a handful of custom block types) is narrow enough that
    # a bespoke parser is easier to debug than fitting a third-party
    # renderer to our reportlab style system.
    md_path = REPO_ROOT / "docs" / "overview.md"
    story.extend(render_overview_md(md_path.read_text()))

    doc.build(story,
              onFirstPage=draw_cover_page,
              onLaterPages=draw_interior_page)


# -----------------------------------------------------------------------------
# Markdown → reportlab-flowables renderer.
# -----------------------------------------------------------------------------
# Supported line-level constructs, applied in this order:
#
#   % ...                  — comment line, skipped
#   # Heading              — page-level heading. Triggers a page break
#                            before rendering (except the very first).
#   ## Heading             — section heading within a page.
#   _lead paragraph_       — lead paragraph (s_lead style), used once after
#                            a # heading to make the intro visually distinct.
#                            Detection is "entire paragraph wrapped in _..._".
#   plain text             — body paragraph.
#
# Block constructs (delimited by ::: lines):
#
#   :::feature             — feature_row: first line before ":" is the
#   Title: body text        bold title, rest is the body.
#   :::
#
#   :::image path
#   Optional caption       — image followed by italic caption, wrapped in
#   :::                      KeepTogether so they never split across pages.
#
#   :::stats
#   :::                    — fixed-layout 6-card stats grid.
#
# Inline:
#
#   **bold**  *italic*  `code`   — standard markdown
#   {key}                         — substitute STATS[key] with grouping
#
# Anything else is a body paragraph.
#
# The parser is intentionally dumb about edge cases: no nested blocks,
# no list syntax, no inline links. If we need those, add them. For now
# the Overview doc doesn't use them and adding unused features to the
# parser just makes it harder to debug.
# -----------------------------------------------------------------------------

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD        = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC      = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_SUBST       = re.compile(r"\{([a-z_]+)\}")


def _apply_inline(text: str) -> str:
    """Convert our markdown-ish inline markup to the reportlab XML subset
    used by Paragraph. Also applies {key} substitutions from STATS.

    Order matters: inline `code` first (its contents shouldn't be further
    parsed for bold/italic), then **bold**, then *italic*, then substitutions.
    """
    # Temporarily replace code spans with placeholders so their contents
    # don't get picked up by bold/italic regex.
    codes = []
    def _stash_code(m):
        codes.append(m.group(1))
        return f"\x00CODE{len(codes) - 1}\x00"
    text = _INLINE_CODE.sub(_stash_code, text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)

    # Substitute {key} → STATS[key], formatted with thousands grouping.
    def _subst(m):
        key = m.group(1)
        if key in STATS:
            return _fmt(STATS[key])
        # Unknown key — leave it literally so a broken substitution is
        # visible in the rendered PDF rather than silently disappearing.
        return m.group(0)
    text = _SUBST.sub(_subst, text)

    # Restore code spans using reportlab's Courier font markup.
    for i, code in enumerate(codes):
        # Escape XML special chars in the code content — `<foo>` should
        # render as literal angle brackets, not a bogus tag.
        esc = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CODE{i}\x00",
                            f'<font face="Courier" size="9">{esc}</font>')
    return text


def _is_lead_paragraph(text: str) -> bool:
    """A paragraph wrapped entirely in _..._ is a lead paragraph."""
    return len(text) > 2 and text.startswith("_") and text.endswith("_") \
        and text[1:-1].count("_") == 0  # no stray underscores inside


def _build_stats_grid():
    """The 6-card 'fun stats' grid. Layout matches the pre-refactor
    generator exactly so the PDF is visually identical."""
    row1 = Table([[
        stat_card(_fmt(STATS["releases"]),
                  "numbered releases\nin the changelog"),
        stat_card(_fmt(STATS["python_lines"]),
                  f"lines of Python\nacross {STATS['python_modules']} modules"),
        stat_card(_fmt(STATS["html_lines"]),
                  f"lines of HTML/JS\nacross {STATS['html_pages']} pages"),
    ]], colWidths=[2.05 * inch, 2.05 * inch, 2.05 * inch])
    row2 = Table([[
        stat_card(_fmt(STATS["endpoints"]),
                  "JSON API endpoints\nunder /api/"),
        stat_card(_fmt(STATS["sqlite_tables"]),
                  "SQLite tables,\nWAL mode"),
        stat_card("1", "bash-script ancestor\n(adsbmilitary.sh 1.6.6)"),
    ]], colWidths=[2.05 * inch, 2.05 * inch, 2.05 * inch])
    _grid_style = TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])
    row1.setStyle(_grid_style)
    row2.setStyle(_grid_style)
    return [row1, Spacer(1, 10), row2, Spacer(1, 20)]


def render_overview_md(text: str):
    """Parse the overview markdown and return a list of flowables.

    The caller prepends the cover page. No cover content comes from the
    markdown — the cover uses page-level drawing (background color, big
    title positioning) that's orthogonal to this renderer.
    """
    flowables = []
    lines = text.split("\n")
    i = 0
    first_heading = True

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip blanks and comments
        if not stripped or stripped.startswith("%"):
            i += 1
            continue

        # Page-level heading: # Title
        if stripped.startswith("# "):
            if not first_heading:
                flowables.append(PageBreak())
            first_heading = False
            heading = stripped[2:].strip()
            flowables.append(Paragraph(_apply_inline(heading), s_h1))
            flowables.append(Spacer(1, 4))
            i += 1
            continue

        # Section heading: ## Title
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            flowables.append(Paragraph(_apply_inline(heading), s_h2))
            i += 1
            continue

        # Custom block: :::kind [args]
        # Consume until the closing ::: line.
        if stripped.startswith(":::"):
            header = stripped[3:].strip()  # "feature", "image path", "stats", ...
            block_lines = []
            i += 1
            while i < len(lines) and lines[i].strip() != ":::":
                block_lines.append(lines[i])
                i += 1
            i += 1  # consume closing :::
            flowables.extend(_render_block(header, block_lines))
            continue

        # Anything else is a paragraph. A paragraph is one or more
        # consecutive non-blank non-block lines — we join them so wrapped
        # source lines produce one flowable.
        para_lines = [line]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            nxt_stripped = nxt.strip()
            if (not nxt_stripped
                    or nxt_stripped.startswith("%")
                    or nxt_stripped.startswith("#")
                    or nxt_stripped.startswith(":::")):
                break
            para_lines.append(nxt)
            i += 1
        para_text = " ".join(l.strip() for l in para_lines).strip()
        if not para_text:
            continue
        if _is_lead_paragraph(para_text):
            # Strip the underscores, use lead style
            inner = para_text[1:-1].strip()
            flowables.append(Paragraph(_apply_inline(inner), s_lead))
        else:
            flowables.append(Paragraph(_apply_inline(para_text), s_body))

    return flowables


def _render_block(header: str, body_lines):
    """Render one custom ::: block. `header` is the text right after the
    opening ':::' on the first line; `body_lines` are the raw lines
    between opening and closing delimiters."""
    # feature: "Title: body text" — supplied either on the header line
    # or as the first body line. We accept both shapes.
    if header == "feature" or header.startswith("feature "):
        # Collect body lines into one text block, then split on first ':'
        raw = " ".join(l.strip() for l in body_lines).strip()
        if ":" in raw:
            title, body = raw.split(":", 1)
            return [feature_row(title.strip(), _apply_inline(body.strip()))]
        # Malformed — render as a plain paragraph rather than silently
        # dropping the content.
        return [Paragraph(_apply_inline(raw), s_body)]

    # image <path> [max_h=N.N]
    # <caption>
    if header.startswith("image "):
        # Header: "image screenshot-live.png" or
        #         "image screenshot-live.png max_h=3.8"
        # Parse optional 'max_h=N' suffix so per-image heights can be
        # tuned in the markdown without touching the renderer. This is
        # the one piece of "style" that leaks into the content file,
        # but it's necessary because different screenshots have very
        # different aspect ratios and the single-default render looks
        # bad across all of them.
        parts = header[len("image "):].strip().split()
        path = parts[0]
        max_h = 4.8  # conservative default
        max_w = 6.5
        for p in parts[1:]:
            if p.startswith("max_h="):
                try:
                    max_h = float(p.split("=", 1)[1])
                except ValueError:
                    pass
            elif p.startswith("max_w="):
                try:
                    max_w = float(p.split("=", 1)[1])
                except ValueError:
                    pass
        caption = " ".join(l.strip() for l in body_lines).strip()
        img_path = SCREENSHOT_DIR / path
        # Use KeepTogether so the image and caption can't split across
        # pages — historical pain point with the old hardcoded generator.
        parts_out = [fitted_image(img_path, max_width_in=max_w,
                                  max_height_in=max_h)]
        if caption:
            parts_out.append(Paragraph(_apply_inline(caption), s_caption))
        return [Spacer(1, 6), KeepTogether(parts_out)]

    # stats — fixed-layout 6-card grid
    if header == "stats":
        return [Spacer(1, 10)] + _build_stats_grid()

    # Unknown block kind — render the raw content as a paragraph rather
    # than failing the build. A typo in the markdown shouldn't block a
    # release; it'll just render visibly wrong.
    raw = " ".join(l.strip() for l in body_lines).strip()
    return [Paragraph(f"[unknown block ':::{header}'] {_apply_inline(raw)}", s_body)]



if __name__ == "__main__":
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    build()
    size = OUT_PATH.stat().st_size
    print(f"Built: {OUT_PATH}")
    print(f"Size:  {size / 1024:.1f} KB  |  Version: {VERSION}")
    print(f"Stats: {STATS['releases']} releases, "
          f"{STATS['python_lines']:,} Python lines ({STATS['python_modules']} modules), "
          f"{STATS['html_lines']:,} HTML/JS lines ({STATS['html_pages']} pages), "
          f"{STATS['endpoints']} endpoints, "
          f"{STATS['sqlite_tables']} SQLite tables")
