"""v3.4.180: automatic updates on a schedule (updates.github.auto_apply).

The rules (auto_update.py) on their own, then whole scheduled runs through the
real apply path: a throwaway install, GitHub answered by a locally built release
zip, the restart call stubbed (test_update_apply's harness). Invented data.
"""
import hashlib
import os
import sqlite3
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

import atomic_io
import auto_update
import updater
from config_validator import validate_config
from test_update_apply import (NEW, OLD, _EndpointCase, _release_files, _zip_release,
                               repo_guard)

setUpModule, tearDownModule = repo_guard()


def ts(y, mo, d, h, mi):
    """A local wall-clock time (system timezone) as unix seconds."""
    return datetime(y, mo, d, h, mi).timestamp()


class TestSchedule(unittest.TestCase):
    # 2026-11-12 is a Thursday.

    def test_due_in_the_hour_after_the_time_once_per_day(self):
        due = auto_update.due_run
        self.assertIsNone(due(ts(2026, 11, 12, 2, 59), "daily", "03:00", None))
        self.assertEqual(due(ts(2026, 11, 12, 3, 0), "daily", "03:00", None), "2026-11-12")
        self.assertEqual(due(ts(2026, 11, 12, 3, 59), "daily", "03:00", None), "2026-11-12")
        self.assertIsNone(due(ts(2026, 11, 12, 4, 0), "daily", "03:00", None))
        self.assertIsNone(due(ts(2026, 11, 12, 3, 30), "daily", "03:00", "2026-11-12"))
        self.assertIsNone(due(ts(2026, 11, 12, 15, 0), "daily", "03:00", "2026-11-11"),
                          "a missed run isn't caught up in the afternoon")

    def test_one_day_a_week(self):
        due = auto_update.due_run
        self.assertEqual(due(ts(2026, 11, 12, 3, 5), "thu", "03:00", None), "2026-11-12")
        self.assertIsNone(due(ts(2026, 11, 12, 3, 5), "fri", "03:00", None))

    def test_a_window_that_crosses_midnight_belongs_to_the_day_it_started(self):
        self.assertEqual(auto_update.due_run(ts(2026, 11, 13, 0, 10), "thu", "23:30", None),
                         "2026-11-12")

    def test_next_run(self):
        nxt = auto_update.next_run
        self.assertEqual(nxt(ts(2026, 11, 12, 15, 0), "daily", "03:00", "2026-11-12"),
                         ts(2026, 11, 13, 3, 0))
        self.assertEqual(nxt(ts(2026, 11, 12, 15, 0), "sun", "03:00", None), ts(2026, 11, 15, 3, 0))
        self.assertEqual(nxt(ts(2026, 11, 12, 3, 20), "daily", "03:00", None),
                         ts(2026, 11, 12, 3, 20), "a run that's due counts as now")
        self.assertEqual(nxt(ts(2026, 11, 12, 3, 20), "daily", "03:00", "2026-11-12"),
                         ts(2026, 11, 13, 3, 0))

    def test_settings_defaults_and_bad_values(self):
        s = auto_update.settings
        self.assertEqual(s(None), {"enabled": False, "day": "daily", "time": "03:00"})
        self.assertEqual(s({"auto_apply": {"enabled": True, "day": "sun", "time": "01:30"}}),
                         {"enabled": True, "day": "sun", "time": "01:30"})
        self.assertEqual(s({"auto_apply": {"enabled": "yes", "day": "someday", "time": "25:00"}}),
                         {"enabled": False, "day": "daily", "time": "03:00"})
        for bad in ("3:00", "03:60", "0300", None, 300, "ab:cd"):
            self.assertIsNone(auto_update.parse_hhmm(bad), bad)


class TestReconcile(unittest.TestCase):
    T0 = 1794000000

    def pending(self):
        return {"pending": {"from": "3.4.179", "to": "3.4.180", "at": self.T0}}

    def test_waits_while_the_update_is_being_confirmed(self):
        st = self.pending()
        self.assertFalse(auto_update.reconcile(st, None, {"state": "pending_first_boot"}, "3.4.180",
                                               self.T0 + 9999))
        self.assertTrue(st["pending"])

    def test_applied(self):
        st = self.pending()
        lr = {"result": "applied", "from_version": "3.4.179", "to_version": "3.4.180", "at": self.T0 + 60}
        self.assertTrue(auto_update.reconcile(st, lr, None, "3.4.180", self.T0 + 70))
        self.assertIsNone(st["pending"])
        self.assertEqual((st["last"]["result"], st["last"]["from"], st["last"]["to"]),
                         ("installed", "3.4.179", "3.4.180"))
        self.assertEqual(st["announce"]["title"], "Aerodrome updated itself to 3.4.180")
        self.assertFalse(auto_update.skips(st, "v3.4.180"))

    def test_rolled_back_is_skipped_from_then_on(self):
        st = self.pending()
        lr = {"result": "rolled_back", "to_version": "3.4.180", "at": self.T0 + 60, "reason": "crashed"}
        self.assertTrue(auto_update.reconcile(st, lr, None, "3.4.179", self.T0 + 70))
        self.assertEqual(st["last"]["result"], "rolled_back")
        self.assertTrue(auto_update.skips(st, "v3.4.180"))
        self.assertFalse(auto_update.skips(st, "v3.4.181"))
        self.assertIn("didn't start", st["announce"]["body"])

    def test_an_older_result_for_the_same_version_doesnt_count(self):
        st = self.pending()
        lr = {"result": "rolled_back", "to_version": "3.4.180", "at": self.T0 - 600}
        self.assertFalse(auto_update.reconcile(st, lr, None, "3.4.179", self.T0 + 70))

    def test_no_result_but_running_the_new_version_is_installed(self):
        st = self.pending()
        self.assertTrue(auto_update.reconcile(st, None, None, "3.4.180", self.T0 + 70))
        self.assertEqual(st["last"]["result"], "installed")

    def test_no_result_for_an_hour_gives_up(self):
        st = self.pending()
        self.assertFalse(auto_update.reconcile(st, None, None, "3.4.179", self.T0 + 600))
        self.assertTrue(auto_update.reconcile(st, None, None, "3.4.179", self.T0 + 3700))
        self.assertEqual(st["last"]["result"], "failed")
        self.assertNotIn("announce", st)
        self.assertFalse(auto_update.skips(st, "3.4.180"))

    def test_skip_list_is_short_and_unique(self):
        st = {}
        for v in ("3.4.180", "3.4.181", "3.4.180", "3.4.182", "3.4.183", "3.4.184", "3.4.185"):
            auto_update._skip(st, v)
        self.assertEqual(st["skip"], ["3.4.181", "3.4.180", "3.4.182", "3.4.183", "3.4.184", "3.4.185"][-5:])

    def test_passing_problems_send_nothing(self):
        for result in ("busy", "failed", "check_failed", "current", "skipped"):
            st = {}
            auto_update.record(st, result, self.T0, "3.4.179", "3.4.180", "why")
            self.assertNotIn("announce", st, result)

    def test_an_old_announcement_is_dropped(self):
        st = {"announce": {"title": "t", "body": "b", "at": self.T0}}
        self.assertIsNone(auto_update.take_announcement(st, self.T0 + 2 * 86400))
        self.assertNotIn("announce", st)


class TestValidator(unittest.TestCase):

    def errors(self, auto):
        return [e for e in validate_config({"updates": {"github": {"auto_apply": auto}}})
                if e[0].startswith("updates.")]

    def test_good(self):
        self.assertEqual(self.errors({"enabled": True, "day": "sun", "time": "03:00"}), [])
        self.assertEqual(self.errors({"enabled": False}), [])

    def test_bad(self):
        paths = {p for p, _ in self.errors({"enabled": "yes", "day": "someday", "time": "3am"})}
        self.assertEqual(paths, {"updates.github.auto_apply.enabled", "updates.github.auto_apply.day",
                                 "updates.github.auto_apply.time"})
        self.assertEqual([p for p, _ in self.errors("on")], ["updates.github.auto_apply"])


class TestScheduledRun(_EndpointCase):
    """Whole scheduled runs: check → download → checksum → stage → the Apply
    button's own staged apply. urlopen serves GitHub from a local release zip."""

    def setUp(self):
        super().setUp()
        tag = "v" + NEW
        self.zip = _zip_release(_release_files(NEW), wrapper=f"aerodrome-{tag}")
        sha = hashlib.sha256(self.zip).hexdigest()
        self.assets = {"latest": ('{"tag_name": "%s"}' % tag).encode(),
                       f"aerodrome-{tag}.zip": self.zip,
                       f"aerodrome-{tag}.zip.sha256": f"{sha}  aerodrome-{tag}.zip\n".encode()}
        self.urls = []
        for item in (self.inst / "update").iterdir():         # nothing staged locally
            if item.name != "UPDATE_README.md":
                updater._remove(item)
        self.state_file = Path(self.db).parent / auto_update.STATE_NAME
        self.notifier = mock.Mock()
        self.notifier._in_quiet_hours.return_value = False
        for p in (mock.patch.object(self.server, "_NOTIFIER", self.notifier),
                  mock.patch("urllib.request.urlopen", self.urlopen)):
            p.start()
            self.addCleanup(p.stop)
        self.schedule(minutes_ago=5)

    def urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.urls.append(url)
        m = mock.MagicMock()
        m.read.return_value = self.assets[url.rsplit("/", 1)[1]]
        m.__enter__.return_value = m
        return m

    def schedule(self, minutes_ago, enabled=True, ntfy=True):
        at = datetime.now() - timedelta(minutes=minutes_ago)
        self.server.CONFIG["stats"] = {"timezone": ""}
        self.server.CONFIG["updates"] = {"github": {
            "enabled": True, "poll_interval": "monthly",
            "notify": {"ntfy": ntfy},
            "auto_apply": {"enabled": enabled, "day": "daily", "time": at.strftime("%H:%M")}}}

    def tick(self):
        self.app.state.auto_update_tick()
        return auto_update.load_state(self.state_file)

    def version(self):
        return (self.inst / "VERSION").read_text().strip()

    def test_a_newer_release_is_installed(self):
        st = self.tick()
        self.assertEqual(self.version(), NEW)
        self.restart.assert_called_once()
        self.assertEqual(st["pending"]["from"], OLD)
        self.assertEqual(st["pending"]["to"], NEW)
        self.assertEqual(st["last_run_date"], (datetime.now() - timedelta(minutes=5)).date().isoformat())
        self.assertEqual(updater.pending(self.inst)["state"], "pending_first_boot")
        self.notifier.notify.assert_not_called()          # its own check says nothing

    def test_it_runs_once_per_scheduled_day(self):
        self.tick()
        updater._remove(self.inst / updater.PENDING)       # as if confirmed...
        atomic_io.write_json_atomic(self.inst / updater.LAST_RESULT, {
            "result": "applied", "from_version": OLD, "to_version": NEW, "at": time.time() + 1})
        st = self.tick()                                  # ...the next tick says so
        self.assertEqual(st["last"]["result"], "installed")
        self.assertIsNone(st["pending"])
        self.notifier.notify.assert_called_once()
        self.assertEqual(self.notifier.notify.call_args.kwargs["title"],
                         f"Aerodrome updated itself to {NEW}")
        n = len(self.urls)
        self.tick()
        self.assertEqual(len(self.urls), n, "no second run in the same window")

    def test_the_message_waits_for_quiet_hours_to_end(self):
        st = {"announce": {"title": "t", "body": "b", "at": int(time.time())}}
        auto_update.save_state(self.state_file, st)
        self.schedule(minutes_ago=180)
        self.notifier._in_quiet_hours.return_value = True
        self.assertIn("announce", self.tick())
        self.notifier.notify.assert_not_called()
        self.notifier._in_quiet_hours.return_value = False
        self.assertNotIn("announce", self.tick())
        self.notifier.notify.assert_called_once()

    def test_the_message_is_dropped_when_update_pushes_are_off(self):
        auto_update.save_state(self.state_file, {"announce": {"title": "t", "body": "b",
                                                               "at": int(time.time())}})
        self.schedule(minutes_ago=180, ntfy=False)
        self.assertNotIn("announce", self.tick())
        self.notifier.notify.assert_not_called()

    def test_up_to_date_downloads_nothing(self):
        self.assets["latest"] = ('{"tag_name": "v%s"}' % OLD).encode()
        st = self.tick()
        self.assertEqual(st["last"]["result"], "current")
        self.assertEqual(len(self.urls), 1)
        self.restart.assert_not_called()

    def test_a_release_that_was_put_back_is_skipped(self):
        auto_update.save_state(self.state_file, {"skip": [NEW]})
        st = self.tick()
        self.assertEqual(st["last"]["result"], "skipped")
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(self.version(), OLD)

    def test_a_refused_apply_is_skipped_from_then_on_and_announced(self):
        self.restart.return_value = (False, "sudo: a password is required")
        st = self.tick()
        self.assertEqual(self.version(), OLD)
        self.assertEqual(st["last"]["result"], "refused")
        self.assertIn("can't restart itself", st["last"]["reason"])
        self.assertIsNone(st["pending"])
        self.assertTrue(auto_update.skips(st, NEW))
        self.notifier.notify.assert_called_once()
        self.assertIsNone(self.server._MAINT.holder())

    def test_a_bad_download_is_tried_again_next_time(self):
        self.assets["aerodrome-v" + NEW + ".zip.sha256"] = b"0" * 64 + b"  x.zip\n"
        st = self.tick()
        self.assertEqual(st["last"]["result"], "failed")
        self.assertFalse(auto_update.skips(st, NEW))
        self.assertEqual(self.version(), OLD)
        self.notifier.notify.assert_not_called()
        self.restart.assert_not_called()

    def test_busy_is_tried_again_next_time(self):
        tok = self.server._MAINT.try_acquire("a backup")
        self.addCleanup(self.server._MAINT.release, tok)
        st = self.tick()
        self.assertEqual(st["last"]["result"], "busy")
        self.assertIn("backup is running", st["last"]["reason"])
        self.assertEqual(self.version(), OLD)

    def test_github_unreachable(self):
        def down(req, timeout=None):
            import urllib.error
            raise urllib.error.URLError("no route")
        with mock.patch("urllib.request.urlopen", down):
            st = self.tick()
        self.assertEqual(st["last"]["result"], "check_failed")
        self.assertIn("no route", st["last"]["reason"])

    def test_nothing_happens_outside_the_hour_or_when_off(self):
        self.schedule(minutes_ago=61)
        self.assertNotIn("last", self.tick())
        self.schedule(minutes_ago=5, enabled=False)
        self.assertNotIn("last", self.tick())
        self.assertEqual(self.urls, [])

    def test_the_card(self):
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO update_state (id, last_check_ts, last_check_result, "
                  "last_known_latest, last_known_latest_ts, updated_at) VALUES (1, 1, 'success', ?, 1, 1)",
                  ("v" + NEW,))
        c.commit()
        c.close()
        auto_update.save_state(self.state_file, {"skip": [NEW], "last_run_date": "2000-01-01"})
        j = self.ep("/api/updates/github/check", "GET")()
        a = j["auto"]
        self.assertEqual((a["enabled"], a["active"], a["day"], a["skips_latest"]), (True, True, "daily", True))
        self.assertLessEqual(a["next_ts"], time.time() + 1)    # due now
        self.server.CONFIG["updates"]["github"]["enabled"] = False
        a = self.ep("/api/updates/github/check", "GET")()["auto"]
        self.assertEqual((a["enabled"], a["active"], a["next_ts"]), (True, False, None))


if __name__ == "__main__":
    unittest.main()
