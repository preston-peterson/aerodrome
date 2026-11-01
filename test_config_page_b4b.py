"""Static test for templates/config.html (B4b B6, the approved Settings page).

Usage: python3 -m unittest test_config_page_b4b
(CONFIG_HTML=<path to config.html> points it at another copy).

Checks: every inline <script> parses (node --check, as bump-version.sh's JS
gate does); the page reads /api/maintenance/status and acks kind "config";
each B0 state's approved wording is there; every server value that reaches
innerHTML in the recovery notice is escaped; a path-less save error shows
its own text; the backup download pre-checks busy and can't be double
clicked; uploads are size-checked in the page; refusals render
message || error || detail; alertModal() has its styles (the page's own
alert-modal rules, which test_ntfy_yaml checks in detail).
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(os.environ.get("CONFIG_HTML")
            or Path(__file__).resolve().parent / "templates" / "config.html")
NODE = shutil.which("node") or shutil.which("nodejs")


class TestProposedConfigPage(unittest.TestCase):

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

    def test_reads_maintenance_status_and_acks_config(self):
        self.assertIn("fetch('/api/maintenance/status')", self.html)
        self.assertIn("JSON.stringify({kind: 'config'})", self.html)
        self.assertIn('id="configRecoveryBanner"', self.html)
        self.assertIn('id="configRecoveryDismiss"', self.html)
        self.assertIn("if (!c || c.acknowledged)", self.html)

    def test_b0_wording(self):
        for text in (
            # 1 recovery notice
            "config.yaml couldn't be read when Aerodrome started (",
            "It's running on the last settings that worked ",
            "; web port ${escapeAttr(c.web_port)}).",
            " Your file was kept as <code>",
            # 2 bind case
            "Aerodrome couldn't listen on ${escapeAttr(c.tried || 'the address')} from config.yaml, ",
            "so it started on the last settings that worked ",
            # 5 download unavailable while busy
            "Download unavailable: ${_busyText(b)}",
            "is running (started ${_agoText(b && b.age_sec)}). Try again when it finishes.",
            "The backup download isn't available right now. ",
            # 6 uploads refused in the page (the server's own refusals render as sent)
            "This backup is larger than 2 GB.",
            "This file is too large to be a config.yaml (the limit is 256 KB). ",
        ):
            self.assertIn(text, self.html, text)

    def test_recovery_notice_escapes_every_server_value(self):
        start = self.html.index("function renderConfigRecovery()")
        body = self.html[start:self.html.index("async function dismissConfigRecovery()")]
        for m in re.finditer(r"\$\{([^}]*)\}", body):
            expr = m.group(1).strip()
            self.assertTrue(expr.startswith("escapeAttr(") or expr in ("src", "kept"),
                            f"unescaped interpolation: ${{{expr}}}")

    def test_a_pathless_save_error_shows_its_own_text(self):
        self.assertIn("if (errs.length === 1 && !errs[0].path) {", self.html)
        self.assertIn("toast(errs[0].message || j.message || j.error || 'Save failed', 'err');",
                      self.html)

    def test_backup_download(self):
        start = self.html.index("async function downloadFullBackup(btn)")
        body = self.html[start:self.html.index("function onFullRestoreFileChosen")]
        self.assertIn("const m = await loadMaintenanceStatus();", body)
        self.assertIn("a.download = '';", body)
        self.assertIn("btn.disabled = true;", body)
        self.assertIn("}, 5000);", body)
        self.assertIn('id="fbDownloadBtn" onclick="downloadFullBackup(this)"', self.html)

    def test_upload_size_checks_and_refusal_text(self):
        self.assertIn("if (file.size > 2 * 1024 * 1024 * 1024) {", self.html)
        self.assertIn("if(file.size > 256 * 1024){", self.html)
        self.assertIn("j.message || j.error || j.detail || 'Unknown error. Check logs for details.'",
                      self.html)
        self.assertIn("let msg = j.message || j.error || j.detail || 'Upload failed';", self.html)

    def test_alert_modal_has_its_styles(self):
        # The page's own alert-modal rules (it never loads index.html's
        # .modal-bg/.modal CSS).
        for sel in (".alert-modal-bg {", ".alert-modal {", ".alert-modal .modal-title {",
                    ".alert-modal .modal-actions {", ".alert-modal .modal-btn {"):
            self.assertIn(sel, self.html, sel)

    def test_phone_width(self):
        # The kept file's path must wrap (Dismiss stays on screen at 360 px),
        # and the modal keeps a gutter of at least 16 px: the backdrop is
        # padded and the box is never wider than it.
        self.assertIn(".banner.notice-banner .text { color:var(--t1); line-height:1.55; "
                      "min-width:0; overflow-wrap:anywhere; }", self.html)
        box = self.html[self.html.index(".alert-modal {"):]
        self.assertIn("width:100%;", box[:box.index("}")])
        modal_bg = self.html[self.html.index(".alert-modal-bg {"):]
        self.assertIn("padding:20px;", modal_bg[:modal_bg.index("}")])

    def test_the_warn_toast_is_amber(self):
        self.assertIn(".toast.warn { border-color:var(--amber); }", self.html)


if __name__ == "__main__":
    unittest.main()
