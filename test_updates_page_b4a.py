"""Static test for templates/updates.html (B4a A5, the approved Updates page).

Usage: python3 -m unittest test_updates_page_b4a
(UPDATES_HTML=<path to updates.html> points it at another copy).

Checks: every inline <script> parses (node --check, as bump-version.sh's JS
gate does); the page reads /api/maintenance/status and renders each A0
state's approved wording; the dead pip_ok branch is gone; both confirm
dialogs describe the staged apply.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(os.environ.get("UPDATES_HTML")
            or Path(__file__).resolve().parent / "templates" / "updates.html")
NODE = shutil.which("node") or shutil.which("nodejs")


class TestProposedUpdatesPage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text()

    @unittest.skipUnless(NODE, "needs node")
    def test_inline_scripts_parse(self):
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", self.html)
        self.assertTrue(blocks)
        with tempfile.TemporaryDirectory() as d:
            for i, b in enumerate(blocks):
                p = Path(d) / f"b{i}.js"
                p.write_text(b)
                r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"script block {i}: {r.stderr[-800:]}")

    def test_reads_maintenance_status_and_acks(self):
        self.assertIn("fetch('/api/maintenance/status')", self.html)
        self.assertIn("/api/maintenance/ack", self.html)
        self.assertIn('id="maintBanner"', self.html)

    def test_a0_wording(self):
        for text in (
            # 1 refused apply
            "The update is still staged — fix the cause and press Apply again.",
            # 3 rolled back (+ schema line)
            "didn't start (${n} attempt",
            "already upgraded the database",
            "works with it.",
            # 4 confirming
            "confirming it started cleanly (about 30 s).",
            # 5 busy
            "is running (started ${_ago(b.age_sec)}).",
            # 6 pip warning
            "Installing Python packages reported a problem; ",
            "the new version loaded anyway.",
            # 7 timeout
            "Still starting — the first start after a big ",
            "upgrade can take several minutes. This page keeps checking.",
            # 8 preflight
            "Updates can't be applied yet:",
            "writable by the account Aerodrome runs as. ",
            # C0-10
            "Applying…",
        ):
            self.assertIn(text, self.html, text)

    def test_confirm_dialogs_describe_the_staged_apply(self):
        self.assertEqual(self.html.count("put this version back automatically"), 2)
        self.assertNotIn("Copy new files over", self.html)
        self.assertNotIn("Reinstall Python dependencies", self.html)
        self.assertNotIn("Back up the current install to .backups", self.html)

    def test_a_pending_update_blocks_the_update_controls(self):
        # The restarted process holds no lock; the journal alone makes the
        # server refuse update operations (409 update_pending).
        self.assertIn("m.update_pending ? {op: 'confirming the last update', parked: true}",
                      self.html)

    def test_dead_pip_branch_is_gone(self):
        self.assertNotIn("j.pip_ok", self.html)
        self.assertNotIn("j.pip_msg", self.html)
        self.assertIn("j.pip_warning", self.html)

    def test_refusal_detail_is_escaped(self):
        # every server string that reaches innerHTML goes through _esc()
        self.assertIn("<pre>${_esc(detail)}</pre>", self.html)
        self.assertIn("${_esc(msg)}", self.html)


if __name__ == "__main__":
    unittest.main()
