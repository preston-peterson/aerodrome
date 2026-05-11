#!/usr/bin/env python3
"""
Admin-page consistency audit harness.

Renders every admin page using the same Playwright + mock-data infrastructure
as scripts/screenshots.py, but instead of producing one PNG per page for
documentation, it crops each page to its header region (top ~140px) and
composes them into a single vertical strip. Drift in header structure —
extra elements, missing buttons, different spacing, off-by-one alignment —
is instantly visible when stacked.

Why this exists: across v2.84.1-v2.84.6 (Apr 2026) Aerodrome shipped six
consecutive releases with admin-page header inconsistencies caught only
by manual user review. Manual audits keep failing because each admin page
is reviewed in its own browser tab; structural drift is invisible to the
eye unless pages are next to each other. This harness puts them next to
each other.

Usage (from repo root):
    pip install -r requirements-dev.txt
    playwright install chromium
    python3 scripts/admin_audit.py

Output:
    screenshots/admin-audit/admin-audit-strip.png — vertical strip of headers
    screenshots/admin-audit/admin-audit-grid.html — side-by-side grid for
        in-browser viewing (resizable, links to individual page captures)

When to run: before shipping any change that touches an admin-page template,
or when refactoring the shared header pattern. The strip should look
near-identical row to row; any visible drift is the bug.

Pages with INTENTIONALLY different header structures (e.g., a public-facing
page that doesn't get the gear menu) should NOT be included in ADMIN_PAGES.
The audit is for pages that should match each other; intentional outliers
are out of scope.
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright
from PIL import Image, ImageDraw, ImageFont

# Reuse the heavy lifting from screenshots.py — mock data, fetch stubs,
# template render helper. Importing rather than reimplementing because
# all that machinery is already known-working and stays in sync with
# the templates as they evolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenshots import _render  # noqa: E402

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / 'screenshots' / 'admin-audit'
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

# Header region: top 140px is enough to capture the full nav bar, gear
# menu, and any below-nav title or breadcrumb that varied in the
# v2.84.x bug class. If a page's header genuinely needs more vertical
# space, bump this — but the audit is most useful when every page's
# captured region is the same height so direct comparison works.
HEADER_HEIGHT = 140

# Standard viewport for capture. Width matches what the documentation
# screenshots use, so users running both harnesses see consistent
# layouts. Height only needs to be big enough that the captured
# top region is fully rendered (HEADER_HEIGHT + some breathing room).
VIEWPORT = {'width': 1400, 'height': 700}

# Pages to audit. Each entry: (template_name, page_label, ready_fn or None).
#
# Inclusion rule: pages that should share the common admin header pattern
# (Aerodrome logo, nav tabs, version+timestamp on the right, gear menu).
# Exclusion rule: pages with intentionally bespoke structure — the public
# Live/Watchlist/Military/Stats/Search tabs (different chrome by design),
# the setup-guide (one-time onboarding), and the per-aircraft detail page
# (deep-link route, different header semantics).
#
# When new admin pages get added, append them here. When an existing page
# is refactored to share the standard header, it should already be in
# this list — the audit catches the drift.
ADMIN_PAGES = [
    ('status.html',                  'Status',                    None),
    ('config.html',                  'Configuration',             None),
    ('logs.html',                    'Logs',                      None),
    ('performance.html',             'Performance',               None),
    ('docs.html',                    'Documentation',             None),
    ('diagnostics.html',             'Diagnostics hub',           None),
    ('diagnostics-slow-queries.html', 'Diagnostics: Slow queries', None),
    ('diagnostics-watchlist.html',   'Diagnostics: Watchlist',    None),
]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

async def _capture_header(browser, template_name: str, label: str, ready_fn):
    """Render a page and return its top HEADER_HEIGHT pixels as a PIL Image.

    Uses screenshots.py's _render helper so all the mock-data and fetch-stub
    setup works identically. The clip parameter on Playwright's screenshot()
    lets us snap just the header region without cropping after the fact.
    """
    out_path = AUDIT_DIR / f'__tmp_{template_name.replace(".", "_")}.png'
    await _render(
        browser,
        template_name,
        out_path,
        viewport=VIEWPORT,
        ready_fn=ready_fn,
        clip={
            'x': 0,
            'y': 0,
            'width': VIEWPORT['width'],
            'height': HEADER_HEIGHT,
        },
    )
    img = Image.open(out_path).convert('RGB')
    out_path.unlink()  # cleanup tmp; the strip image is the deliverable
    return img


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def _compose_strip(captures):
    """Compose captured headers into a single vertical strip with labels.

    Each row: a 32px label band (page name), then the header capture, then
    a 4px separator. The label band uses the same dark theme as Aerodrome's
    UI so the composite reads as a cohesive layout, not a slideshow.

    Returns a PIL Image. Caller saves wherever they want.
    """
    LABEL_HEIGHT = 32
    SEP_HEIGHT = 4
    width = VIEWPORT['width']
    total_height = sum(
        LABEL_HEIGHT + img.height + SEP_HEIGHT
        for _, img in captures
    )
    strip = Image.new('RGB', (width, total_height), (20, 20, 28))
    draw = ImageDraw.Draw(strip)
    # Try a bold sans for labels; fall back to PIL default if DejaVu isn't
    # installed (CI environments without fontconfig). Default font is fine
    # for an audit harness — readability matters more than aesthetics.
    try:
        font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 16
        )
    except (IOError, OSError):
        font = ImageFont.load_default()

    y = 0
    for label, img in captures:
        # Label band — slightly lighter than background for contrast.
        draw.rectangle([0, y, width, y + LABEL_HEIGHT], fill=(40, 44, 60))
        draw.text((12, y + 8), label, fill=(220, 220, 230), font=font)
        y += LABEL_HEIGHT
        # Header capture
        strip.paste(img, (0, y))
        y += img.height
        # Separator — accent color so row boundaries are obvious
        draw.rectangle([0, y, width, y + SEP_HEIGHT], fill=(80, 130, 200))
        y += SEP_HEIGHT
    return strip


def _write_grid_html(captures, html_path):
    """Write an HTML page that lays out individual captures in a scrollable
    column with labels. Useful when the maintainer wants to inspect at full
    resolution in a browser instead of in an image viewer.

    The HTML embeds the PNGs as relative <img src> references; viewers
    should keep the captures and the HTML in the same directory. Each
    capture is also saved out individually so the HTML works.
    """
    rows_html = []
    for label, img in captures:
        slug = label.lower().replace(': ', '-').replace(' ', '-')
        png_name = f'header-{slug}.png'
        img.save(AUDIT_DIR / png_name)
        rows_html.append(
            f'<div class="row"><div class="lbl">{label}</div>'
            f'<img src="{png_name}" alt="{label}"></div>'
        )
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Admin page audit</title>
<style>
  body { background: #14141c; color: #e0e0e6; font: 14px sans-serif; margin: 0; padding: 16px; }
  h1 { font-weight: 500; margin: 0 0 16px 0; }
  .row { margin-bottom: 16px; border: 1px solid #2c2c40; border-radius: 4px; overflow: hidden; }
  .lbl { background: #28283c; padding: 8px 12px; font-weight: 600; }
  .row img { display: block; width: 100%; height: auto; }
</style></head><body>
<h1>Aerodrome admin page header audit</h1>
<p>Each row is the top """ + str(HEADER_HEIGHT) + """px of one admin page, rendered with mock data via Playwright. Look for inconsistencies in header structure, gear menu placement, title formatting, and spacing.</p>
""" + '\n'.join(rows_html) + """
</body></html>
"""
    html_path.write_text(html)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print(f"Admin-page header audit → {AUDIT_DIR}/")
    print(f"Pages: {len(ADMIN_PAGES)}\n")

    captures = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for template, label, ready in ADMIN_PAGES:
            print(f"  → {label}")
            try:
                img = await _capture_header(browser, template, label, ready)
                captures.append((label, img))
            except Exception as e:
                print(f"    ! failed: {e}")
        await browser.close()

    if not captures:
        print("\nNo captures succeeded. Check that templates render under the screenshots.py mock data.")
        return 1

    # Strip
    strip = _compose_strip(captures)
    strip_path = AUDIT_DIR / 'admin-audit-strip.png'
    strip.save(strip_path)
    print(f"\n  ✓ {strip_path.name}  ({strip.width}×{strip.height})")

    # Grid HTML for browser viewing
    grid_path = AUDIT_DIR / 'admin-audit-grid.html'
    _write_grid_html(captures, grid_path)
    print(f"  ✓ {grid_path.name}")

    print()
    print("Look for:")
    print("  • inconsistent header bar structure (different elements present/missing)")
    print("  • inconsistent gear menu position or contents")
    print("  • inconsistent title/breadcrumb formatting")
    print("  • differing vertical alignment or padding")
    print()
    print("Pages with intentional structural differences should be removed")
    print("from ADMIN_PAGES at the top of this script.")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()) or 0)
