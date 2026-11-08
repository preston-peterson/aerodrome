"""Red, amber and green text uses the --red-text / --amber-text / --green-text
shades (v3.4.173). The plain --red / --amber / --green are fills for dots, bars
and borders; as text on white they are 3.8:1, 2.2:1 and 3.3:1, under the 4.5:1
that small text needs. The display board keeps its own dark-only palette."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAIN_AS_TEXT = re.compile(
    r"(?<![-\w])color\s*:\s*var\(--(?:red|amber|green)\s*[,)]"          # CSS / inline style
    r"|\.style\.color\s*=\s*['\"`]var\(--(?:red|amber|green)\s*[,)]")    # JS assignment


class TestTextShades(unittest.TestCase):
    def test_no_plain_red_amber_or_green_text(self):
        files = sorted(list(ROOT.glob("templates/*.html")) + list(ROOT.glob("static/*.css"))
                       + list(ROOT.glob("static/*.js")))
        found = []
        for f in files:
            if f.name == "board.html":
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if PLAIN_AS_TEXT.search(line):
                    found.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
        self.assertEqual(found, [], "use var(--red-text) / var(--amber-text) / var(--green-text) for text")

    def test_the_text_shades_exist_in_both_themes(self):
        css = (ROOT / "static/theme.css").read_text()
        for shade in ("--red-text", "--amber-text", "--green-text"):
            self.assertEqual(len(re.findall(re.escape(shade) + r"\s*:", css)), 2, shade)


if __name__ == "__main__":
    unittest.main()
