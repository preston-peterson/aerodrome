"""Static test for templates/config.html (B4c C5, the approved Backup &
restore page).

Usage: python3 -m unittest test_config_page_b4c
(CONFIG_HTML=<path to config.html> points it at another copy). B4b's
test_config_page_b4b checks the same page: the B4c page is built on B4b's.

Checks: every inline <script> parses (node --check, as bump-version.sh's JS
gate does); the page reads `restore` from /api/maintenance/status and acks
kind "restore"; each C0 state's approved wording is there; every server value
that reaches innerHTML in the restore notice, the 422 list and the
pre-restore rows is escaped; a 422 with database_only offers the database
alone and resubmits with keep_current_config; Cancel restore and Put this
back call their endpoints; Put this back and Purge all are disabled while a
restore waits; a cancel or an expiry says when the backup's notification-server
settings were already applied; a switch that never happened says the current
data was kept;
the restore texts no longer promise a "one-click undo" (Decision 5a built
it) or say "the backup has been applied" before the restart.
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


def _body(html, start, end):
    a = html.index(start)
    return html[a:html.index(end, a)]


class TestProposedRestoreUI(unittest.TestCase):

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

    def test_reads_restore_status_and_acks_it(self):
        self.assertIn("renderRestoreNotice();", self.html)
        self.assertIn("const st = (_maint && _maint.restore) || {};", self.html)
        self.assertIn("JSON.stringify({kind: 'restore'})", self.html)
        for el in ('id="restoreNotice"', 'id="restoreNoticeText"', 'id="restoreCancelBtn"',
                   'id="restoreDismissBtn"'):
            self.assertIn(el, self.html, el)
        self.assertIn("} else if (res && !res.acknowledged) {", self.html)

    def test_c0_wording(self):
        for text in (
            # 1 the 422 list + database only
            "The backup's settings can't be used here",
            "${e.path || 'config.yaml'} — ${e.message}",
            "Restore database only (keep this machine's settings)",
            # 2 success modal
            "Backup checked — restarting to switch to it",
            "If the restored data doesn\\'t load, your current data is put back automatically. ",
            # 3 banners after the restart
            "Restored ${what}.",
            "The restored backup didn't load (${escapeAttr(res.error)}). ",
            "Your previous data was put back.",
            "taken before the restore is gone, so the previous data could not be put ",
            "because Aerodrome didn't restart within an hour. Restore again if you ",
            "The staged restore was cancelled. Nothing was changed.",
            "The restored backup wasn't switched in (${escapeAttr(res.error)}). ",
            "Your current data was kept as it was.",
            "already applied and stay; nothing else was changed.",
            "The notification server\\'s settings from the backup were already applied ",
            # 4 staged and waiting
            "A restore is staged and waits for a restart${why}. ",
            "Settings can't be changed until then.${until}",
            " (Aerodrome couldn't restart itself: ${escapeAttr(p.restart_failed)})",
            "Cancel restore",
            # 5 Put this back (Decision 5a)
            "Put this back",
            "in use by the waiting restore",
            # texts (C0-9)
            "It is checked first;",
            "Upload an <code>aerodrome-backup-*.zip</code> file.",
            "database location and log folder are kept.",
            "Aerodrome checks the backup first, then restarts to switch to it:",
        ):
            self.assertTrue(text in self.html, text)

    def test_no_old_promises(self):
        for text in ("as a one-click undo", "The backup has been applied.",
                     "This is DESTRUCTIVE", "Upload a <code>"):
            self.assertNotIn(text, self.html, text)

    def test_restore_notice_escapes_every_server_value(self):
        body = _body(self.html, "function renderRestoreNotice()", "async function dismissRestoreNotice()")
        for m in re.finditer(r"\$\{([^}]*)\}", body):
            expr = m.group(1).strip()
            self.assertTrue(expr.startswith("escapeAttr(") or expr in ("what", "why", "until")
                            or expr.startswith("notes.join("),
                            f"unescaped interpolation: ${{{expr}}}")
        self.assertIn("const notes = (res.notes || []).map(n => escapeAttr(n));", body)

    def test_choice_modal_escapes(self):
        body = _body(self.html, "function choiceModal(", "async function _restoreRequest(")
        for m in re.finditer(r"\$\{([^}]*)\}", body):
            expr = m.group(1).strip()
            ok = (expr.startswith("_escHtmlCfg(") or expr in ("a", "aText", "list", "i")
                  or expr.startswith("items.map(") or expr.startswith("buttons.map(")
                  or expr.startswith("b.primary"))
            self.assertTrue(ok, f"unescaped interpolation: ${{{expr}}}")

    def test_pre_restore_rows(self):
        body = _body(self.html, "function _renderPreRestoreItem(s)", "async function loadPreRestoreList()")
        self.assertIn("/^[0-9]{8}-[0-9]{6}$/.test(s.timestamp || '')", body)
        self.assertIn("onclick=\"putBackPreRestore('${ts}', this)\"", body)
        self.assertIn("renderPutBackButtons();", self.html)
        self.assertIn("b.disabled = waiting;", self.html)

    def test_purge_all_is_disabled_while_a_restore_waits(self):
        body = _body(self.html, "function renderPutBackButtons()", "function renderBackupBusy()")
        self.assertIn("const purge = document.getElementById('prPurgeBtn');", body)
        self.assertIn("purge.disabled = true;", body)
        # after loadPreRestoreList enables it, never before
        self.assertIn("btn.disabled = false;\n        renderPutBackButtons();", self.html)

    def test_database_only_resubmits(self):
        body = _body(self.html, "async function _restoreRequest(", "async function _restoreStagedModal(")
        self.assertIn("if (r.status === 422 && j.database_only && !dbOnly) {", body)
        self.assertIn("if (choice === 'db') return _restoreRequest(send, true);", body)
        self.assertIn("'/api/backup/import' + (dbOnly ? '?keep_current_config=true' : '')", self.html)
        self.assertIn("JSON.stringify({path: path, keep_current_config: !!dbOnly})", self.html)

    def test_endpoints(self):
        self.assertIn("fetch('/api/backup/staged-restore/cancel', {method: 'POST'})", self.html)
        self.assertIn("`/api/backup/pre-restore/${encodeURIComponent(ts)}/restore`", self.html)

    def test_refusals_render_the_message(self):
        body = _body(self.html, "async function _restoreRequest(", "async function _restoreStagedModal(")
        self.assertIn("j.message || j.error || j.detail || 'Unknown error. Check logs for details.'",
                      body)


if __name__ == "__main__":
    unittest.main()
