"""Regression: text on a cyan fill must be the page background color.

White on the dark theme's cyan is only 2.4:1 contrast; every other cyan
fill in the app uses var(--bg0) for its text (8.0:1 dark, 5.1:1 light).
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent

CYAN_BG = re.compile(r"background\s*:\s*var\(--cyan\)")
WHITE_FG = re.compile(r"(?<![-\w])color\s*:\s*(#fff(?![0-9a-fA-F])|#ffffff|white)\b", re.I)
BLOCK = re.compile(r"\{([^{}]*)\}")


def white_on_cyan_violations(text):
    out = []
    for block in BLOCK.findall(text):
        if CYAN_BG.search(block) and WHITE_FG.search(block):
            out.append(" ".join(block.split())[:120])
    return out


class TestCyanFillText(unittest.TestCase):
    def test_theme_css(self):
        text = (REPO / "static" / "theme.css").read_text(encoding="utf-8")
        violations = white_on_cyan_violations(text)
        self.assertEqual(violations, [])

    def test_index_html(self):
        text = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
        violations = white_on_cyan_violations(text)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
