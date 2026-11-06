"""The real pages in real Chromium, with every server answer scripted, so a
request can be made to hang, fail, or arrive late.

Usage: python3 -m unittest test_frontend_browser
(FRONTEND_ROOT=<dir holding templates/ and static/> points it at another copy).

Needs playwright and a Chromium from requirements-dev.txt; skipped without them,
and with AERODROME_SKIP_SLOW_TESTS=1. No server runs and nothing leaves the
machine: every request is answered here or refused.
"""
import glob
import json
import os
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(os.environ.get("FRONTEND_ROOT") or Path(__file__).resolve().parent)
BASE = "http://aerodrome.test"
HOLD = object()
PAGES = {"/": "index.html", "/board": "board.html", "/status": "status.html",
         "/updates": "updates.html", "/config": "config.html"}


def _chrome():
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/"
        "chrome-headless-shell-linux64/chrome-headless-shell")))
    return found[-1] if found else None


def _aircraft(n):
    return {"aircraft": [
        {"icao": f"A{i:05d}", "callsign": f"DEMO{i}", "altitude": 12000, "speed": 300,
         "lat": 40.0 + i / 100, "lon": -90.0, "track": 90}
        for i in range(1, n + 1)],
        "receiver": {"lat": 40.0, "lon": -90.0, "distance_unit": "mi"},
        "last_updated": 1700000000}


def _status(**extra):
    ok = {"ok": True}
    answer = {"overall_ok": True, "severity": "ok", "failing": [], "version": "0.0.0",
              "update_available": False, "timestamp": 1700000000,
              "components": {"receiver": ok, "database": ok, "collector": ok,
                             "webserver": ok, "hexdb_resolver": ok},
              "system": {"load_average": {"15m": 0.1}, "cpu_cores": 4}}
    answer.update(extra)
    return answer


class Site:
    """Answers everything the page asks for. `api` maps a path (or a path prefix
    ending in *) to a dict, a list, (status, text), or a function of the request
    returning one of those or HOLD."""

    def __init__(self, page):
        self.page = page
        self.api = {}
        self.calls = []
        self.held = []
        self.down = False
        self.finished = []
        page.route("**/*", self._route)

    def _answer_for(self, path):
        if path in self.api:
            return self.api[path]
        for key in sorted(self.api, key=len, reverse=True):
            if key.endswith("*") and path.startswith(key[:-1]):
                return self.api[key]
        return {"ok": True}

    def _route(self, route):
        req = route.request
        url = urlparse(req.url)
        if url.netloc != "aerodrome.test" or self.down:
            route.abort("connectionrefused")
            return
        path = url.path
        if path.startswith("/static/"):
            route.fulfill(path=str(ROOT / path.lstrip("/")))
        elif path.startswith("/api/"):
            self.calls.append((path, parse_qs(url.query)))
            answer = self._answer_for(path)
            if callable(answer):
                answer = answer(req)
            if answer is HOLD:
                self.held.append(route)
            else:
                self._fulfill(route, answer)
        elif path in PAGES or path.startswith("/aircraft/"):
            name = PAGES.get(path, "aircraft.html")
            # The server adds the time helpers to every page it sends.
            html = (ROOT / "templates" / name).read_text().replace(
                "</head>", '<script src="/static/timefmt.js"></script></head>', 1)
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            route.abort("connectionrefused")

    def _fulfill(self, route, answer):
        try:
            if isinstance(answer, tuple):
                route.fulfill(status=answer[0], content_type="text/html", body=answer[1])
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(answer))
        except Exception:
            pass    # the page gave up on this request already

    def release(self, answer):
        """Answer every held request with `answer`, or a function of the request."""
        held, self.held = self.held, []
        for route in held:
            self._fulfill(route, answer(route.request) if callable(answer) else answer)

    def count(self, path):
        return sum(1 for p, _ in self.calls if p == path)


@unittest.skipIf(sync_playwright is None or _chrome() is None, "needs playwright and Chromium")
@unittest.skipIf(os.environ.get("AERODROME_SKIP_SLOW_TESTS") == "1", "slow")
class BrowserCase(unittest.TestCase):
    TIMEZONE = "America/Chicago"
    # Noon, so the board's 4 a.m. reload stays out of the way unless a test wants it.
    START = "2026-06-15T12:00:00-05:00"

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=_chrome())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(timezone_id=self.TIMEZONE, service_workers="block")
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.site = Site(self.page)
        self.page.clock.install(time=self.START)

    def tearDown(self):
        self.context.close()

    def open(self, path, settle=300):
        self.page.goto(BASE + path, wait_until="domcontentloaded")
        self.page.wait_for_timeout(settle)

    def advance(self, ms, step=1000):
        """Move the page's clock on, giving requests a moment after each step."""
        for _ in range(max(1, ms // step)):
            self.page.clock.run_for(step)
            self.page.wait_for_timeout(4)

    def text(self, selector):
        return self.page.inner_text(selector)


class TestBoardNeverLiesAboutBeingLive(BrowserCase):

    def setUp(self):
        super().setUp()
        self.site.api["/api/live"] = _aircraft(2)
        self.open("/board?mode=board")
        self.advance(1000)
        self.assertEqual(self.word(), "LIVE")

    def word(self):
        return self.page.evaluate("document.querySelector('[data-rxword]').textContent")

    def banner(self):
        return self.page.evaluate("document.getElementById('lostBanner').style.display")

    def test_a_request_that_never_answers_shows_no_signal(self):
        self.site.api["/api/live"] = lambda req: HOLD
        self.advance(40000)
        self.assertEqual(self.word(), "NO SIGNAL")
        self.assertEqual(self.banner(), "block")

    def test_it_recovers_after_a_request_that_never_answered(self):
        self.site.api["/api/live"] = lambda req: HOLD
        self.advance(40000)
        self.site.api["/api/live"] = _aircraft(3)
        self.advance(70000)
        self.assertEqual(self.word(), "LIVE")
        self.assertEqual(self.banner(), "none")

    def test_a_long_outage_doesnt_land_on_a_browser_error_page(self):
        self.site.down = True
        self.advance(1200000, step=5000)
        self.assertTrue(self.page.url.startswith(BASE), self.page.url)
        self.assertEqual(self.word(), "NO SIGNAL")
        self.site.down = False
        self.advance(120000, step=5000)
        self.assertTrue(self.page.url.startswith(BASE), self.page.url)
        self.assertEqual(self.word(), "LIVE")

    def test_the_daily_reload_waits_for_the_server(self):
        self.site.down = True
        self.page.clock.fast_forward("16:00:30")       # next day, 04:00:30
        self.advance(5000)
        self.assertTrue(self.page.url.startswith(BASE), self.page.url)
        self.site.down = False
        navigated = []
        self.page.on("framenavigated", lambda f: navigated.append(f.url))
        self.advance(40000)
        self.assertTrue(navigated, "the reload happens once the server answers")
        self.assertTrue(self.page.url.startswith(BASE), self.page.url)

    def test_a_lookup_that_failed_is_tried_again_soon(self):
        path = "/api/aircraft/A00009/photo"
        self.site.api[path] = (500, "boom")
        self.page.evaluate("photoFor('A00009')")
        self.advance(1000)
        self.assertEqual(self.site.count(path), 1)
        self.site.api[path] = {"found": True, "thumbnail_url": "/x.jpg"}
        self.advance(200000, step=5000)
        self.page.evaluate("photoFor('A00009')")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.site.count(path), 2)
        self.assertEqual(self.page.evaluate("photoFor('A00009')"), {"url": "/x.jpg"})

    def test_a_lookup_with_no_answer_is_tried_again_later(self):
        path = "/api/aircraft/A00009/enrich"
        self.site.api[path] = {"registered_owner": None, "registration": None}
        self.page.evaluate("ownerFor('A00009')")
        self.advance(1000)
        self.site.api[path] = {"registered_owner": "Example Aviation", "registration": "N1EX"}
        self.advance(600000, step=5000)
        self.page.evaluate("ownerFor('A00009')")
        self.advance(1000)
        self.assertEqual(self.site.count(path), 1, "not asked again after 10 minutes")
        self.advance(1500000, step=5000)
        self.page.evaluate("ownerFor('A00009')")
        self.advance(1000)
        self.assertEqual(self.site.count(path), 2, "asked again after 35 minutes")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.evaluate("ownerFor('A00009')"), "Example Aviation")

    def test_a_route_changes_when_the_flight_does(self):
        path = "/api/callsign/DEMO7/route"
        leg = lambda a, b: {"found": True, "current_leg": {
            "origin_icao": a, "origin_name": a, "dest_icao": b, "dest_name": b}}
        self.site.api[path] = leg("KAAA", "KBBB")
        ask = "routeFor({callsign: 'DEMO7', lat: 40, lon: -90})"
        self.page.evaluate(ask)
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.evaluate(ask)["stops"][0]["icao"], "KAAA")
        self.site.api[path] = leg("KBBB", "KCCC")
        self.advance(1900000, step=5000)
        # While it asks again, the old answer stays on screen.
        self.assertEqual(self.page.evaluate(ask)["stops"][0]["icao"], "KAAA")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.page.evaluate(ask)["stops"][0]["icao"], "KBBB")


class TestLiveTick(BrowserCase):

    def setUp(self):
        super().setUp()
        self.site.api["/api/live"] = _aircraft(1)
        self.site.api["/api/status"] = _status()
        self.open("/")
        self.advance(1000)

    def count_shown(self):
        return self.text("#cLive")

    def test_a_broken_status_doesnt_freeze_the_list(self):
        self.site.api["/api/status"] = (500, "<html>boom</html>")
        self.site.api["/api/live"] = _aircraft(2)
        self.advance(6000)
        self.assertEqual(self.count_shown(), "2")

    def test_a_status_that_never_answers_doesnt_freeze_the_list(self):
        self.site.api["/api/status"] = lambda req: HOLD
        self.site.api["/api/live"] = _aircraft(2)
        self.advance(6000)
        self.assertEqual(self.count_shown(), "2")

    def test_slow_answers_dont_stack_up(self):
        self.site.api["/api/live"] = lambda req: HOLD
        before = self.site.count("/api/live")
        self.advance(14000)
        self.assertLessEqual(self.site.count("/api/live") - before, 1)

    def test_a_request_that_never_answers_is_given_up_on(self):
        self.site.api["/api/live"] = lambda req: HOLD
        self.advance(21000)
        self.assertIn("off", self.page.get_attribute("#healthDot", "class"))
        self.site.api["/api/live"] = _aircraft(4)
        self.advance(11000)
        self.assertEqual(self.count_shown(), "4")
        self.assertNotIn("off", self.page.get_attribute("#healthDot", "class"))

    def test_status_is_asked_for_on_its_own_schedule(self):
        before = self.site.count("/api/status")
        self.advance(60000)
        # This page's own poll every 30 s; the banner rides it (it was 17 once).
        self.assertLessEqual(self.site.count("/api/status") - before, 5)

    def test_the_list_keeps_what_it_had_when_live_fails(self):
        self.site.api["/api/live"] = (500, "boom")
        self.advance(6000)
        self.assertEqual(self.count_shown(), "1")


class TestLiveRowFocusesRadar(BrowserCase):
    """Clicking a Live row centres the radar on that aircraft. The row's own
    controls (ICAO link, Track link, watchlist button) and text selection
    keep behaving as before. _aircraft() gives lat/lon so the map can place
    the contact."""

    ROW = "#bLive .row-card[data-icao='A00001']"

    def setUp(self):
        super().setUp()
        self.site.api["/api/live"] = _aircraft(1)
        self.site.api["/api/status"] = _status()
        self.site.api["/api/ui-config"] = {
            "track_link_provider": "example_track",
            "track_link_fallback": "example_track",
            "track_link_providers": {"example_track": {
                "label": "Example Track", "url": "http://track.example/{HEX_UPPER}",
                "reg_required": False}}}
        self.open("/")
        self.advance(1000)

    def focused(self):
        return self.page.evaluate("_radar.selectedIcao")

    def overlay_open(self):
        return self.page.evaluate(
            "document.getElementById('radarOverlay').classList.contains('open')")

    def map_shown(self):
        return self.page.evaluate(
            "document.getElementById('liveSplit').classList.contains('show-map')")

    def test_clicking_the_callsign_cell_opens_the_radar_overlay(self):
        self.page.click(self.ROW + " .col-callsign")
        self.advance(200)
        self.assertEqual(self.focused(), "A00001")
        self.assertTrue(self.overlay_open())

    def test_on_a_phone_the_click_shows_the_radar_pane(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        # <=700px the row collapses to ICAO/Description/Track/WL; click a
        # cell that's still on screen.
        self.page.click(self.ROW + " .col-desc")
        self.advance(200)
        self.assertTrue(self.map_shown())

    def test_the_track_link_and_watchlist_button_do_not_move_the_radar(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.click(self.ROW + " .col-desc")       # a plain row click does focus
        self.advance(200)
        self.assertEqual(self.focused(), "A00001")
        self.assertTrue(self.map_shown())
        self.page.evaluate("closeRadarOverlay(); setLiveView('list')")
        self.assertIn("track.example",
                      self.page.get_attribute(self.ROW + " .c-track a", "href"))
        with self.page.expect_popup() as popped:
            self.page.click(self.ROW + " .c-track a")
        popped.value.close()
        self.page.click(self.ROW + " .c-wl button")
        self.advance(200)
        self.assertIsNone(self.focused(), "the row's own controls don't focus the radar")
        self.assertFalse(self.map_shown())

    def test_a_text_selection_inside_the_row_does_not_jump_the_map(self):
        self.page.evaluate("""() => {
            const el = document.querySelector('#bLive .row-card .col-callsign');
            const r = document.createRange(); r.selectNodeContents(el);
            const s = getSelection(); s.removeAllRanges(); s.addRange(r);
        }""")
        self.assertTrue(self.page.evaluate("String(getSelection())"))
        self.page.evaluate("""() => document.querySelector('#bLive .row-card .col-callsign')
            .dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
        self.advance(200)
        self.assertIsNone(self.focused(), "selecting a callsign shouldn't jump the map")
        self.page.evaluate("getSelection().removeAllRanges()")
        self.page.click(self.ROW + " .col-callsign")
        self.advance(200)
        self.assertEqual(self.focused(), "A00001", "a plain click still focuses")


def _status_page(notifications, **extra):
    """A /api/status answer complete enough for the Status page to draw every card."""
    answer = _status(**extra)
    answer["components"] = {
        "receiver": {"ok": True, "url": "http://192.0.2.10:8080/data/aircraft.json",
                     "response_ms": 45, "error": None},
        "database": {"ok": True, "size_mb": 45.0, "error": None,
                     "stats": {"all": {"total": 1000, "unique": 100},
                               "military": {"total": 10, "unique": 2},
                               "watchlist": {"total": 5, "unique": 1}},
                     "capacity": {"ok": True, "db_size_mb": 45.0, "mb_per_day": 3.0,
                                  "bytes_per_row": 170.4, "retention_days": 30,
                                  "projected_settled_mb": 90.0, "disk_free_mb": 12400.0,
                                  "headroom_ratio": 138.3, "what_if": [], "error": None}},
        "collector": {"ok": True, "last_write_seconds_ago": 5, "records_per_sec_5m": 4.2,
                      "records_sample_count_5m": 1260, "error": None},
        "webserver": {"ok": True, "host": "0.0.0.0", "port": 8000, "error": None},
        "hexdb_resolver": {"ok": True, "response_ms": 112, "error": None},
        "notifications": notifications,
    }
    answer["system"] = {"ok": False, "error": "not in this test"}
    return answer


_CONFIG = {
    "receiver": {"ip": "192.0.2.10", "port": 8080, "path": "/data/aircraft.json", "latitude": 37.5,
                 "longitude": -122.1, "poll_interval": 60, "distance_unit": "mi"},
    "web": {"host": "0.0.0.0", "port": 8000},
    "retention": {"all_days": 30, "military_days": 30, "watchlist_days": 30},
    "data": {"db_file": "aircraft_history.db"},
    "logging": {"dir": "logs", "level": "INFO"},
    "military": {"enabled": True, "default_color": "#ef4444", "special_aircraft": {}},
    "watchlist_alerts": {"enabled": True, "trigger": "live", "effect": "pulse_dot", "color": "#f59e0b"},
    "notifications": {"enabled": True, "url": "http://localhost:2586/example-topic",
                      "events": {"receiver_offline": True}},
}


def _ntfy_status(state, stale=False, missing=()):
    """/api/ntfy/status for a state; `missing` names the files a partial install lacks."""
    present = {k: {"present": state in ("aerodrome_managed", "partial") and k not in missing}
               for k in ("binary", "config", "unit")}
    return {"ok": True, "latest_available": "2.11.0", "stale_data": stale,
            "status": dict(present, state=state, installable=state in ("not_installed", "partial"),
                           arch="amd64", version=None if state == "not_installed" else "2.11.0",
                           service={"active": True}, stamp={"present": state != "not_installed"}),
            "config": {"base_url": "http://192.0.2.20:2586", "upstream_relay": True, "problems": []}}


class _ConfigPage(BrowserCase):
    WIDTH = 1280

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PAGES["/config"] = "config.html"

    def open_config(self, ntfy, width=None):
        self.page.set_viewport_size({"width": width or self.WIDTH, "height": 900})
        self.site.api["/api/config"] = _CONFIG
        self.site.api["/api/ntfy/status"] = ntfy
        self.site.api["/api/status"] = _status()
        self.site.api["/api/notifications/stats"] = {"ok": True, "stats": None}
        self.site.api["/api/notifications/recent"] = {"ok": True, "items": []}
        self.open("/config")
        self.advance(1000)

    def tab(self, name):
        self.page.evaluate(f"switchTab({json.dumps(name)})")
        self.advance(500, step=250)
        if name == "notifications":
            self.page.evaluate("nfToggleInstallPanel(true)")
            self.advance(500, step=250)


class TestConfigFitsOnPhones(_ConfigPage):
    """No tab of the Configuration page is wider than a phone's screen. A long
    dropdown option or the desktop 200px field floor used to widen it to 554 px."""

    def wider_than_screen(self):
        return self.page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")

    def test_every_tab_fits(self):
        for width in (320, 390):
            for state in ("not_installed", "partial"):
                self.open_config(_ntfy_status(state, stale=True), width)
                tabs = self.page.evaluate("TABS.map(t => t.id)")
                self.assertIn("notifications", tabs)
                for name in tabs:
                    with self.subTest(width=width, state=state, tab=name):
                        self.tab(name)
                        self.assertLessEqual(self.wider_than_screen(), 0)

    def test_the_cleanup_command_copies_as_written(self):
        self.open_config(_ntfy_status("not_installed", stale=True), 320)
        self.tab("notifications")
        self.assertEqual(self.page.text_content("#nfStaleCacheCmd"),
                         "sudo /usr/local/libexec/aerodrome/aerodrome-helper ntfy-uninstall --purge")


class TestNtfyRepair(_ConfigPage):
    """A partly installed ntfy offers Repair beside Uninstall. Repair keeps the
    phone's topic: it sends the current one and leaves notifications.url alone."""

    def panel(self, missing):
        self.open_config(_ntfy_status("partial", missing=missing))
        self.tab("notifications")
        return self.text("#nfNtfyPanel")

    def test_the_panel_names_what_is_missing(self):
        text = self.panel(("binary",))
        self.assertIn("the ntfy program is missing", text)
        self.assertIn("keeps your settings and topic", text)
        self.assertEqual(self.page.locator("#nfInstallPort").count(), 0, "no fields: the settings are kept")
        self.assertEqual(self.page.locator("#nfNtfyPanel button").all_inner_texts(), ["Repair", "Uninstall"])

    def test_a_missing_settings_file_asks_for_port_and_bind(self):
        text = self.panel(("config", "unit"))
        self.assertIn("its settings file and its service are missing", text)
        self.assertEqual(self.page.locator("#nfInstallPort").count(), 1)

    def test_repair_sends_the_current_topic_and_keeps_the_url(self):
        sent = []
        def install(req):
            sent.append(json.loads(req.post_data))
            return {"ok": True, "repaired": True, "topic": "example-topic", "port": 2586,
                    "version": "2.11.0", "message": "ntfy 2.11.0 repaired and started."}
        self.site.api["/api/ntfy/install"] = install
        self.panel(("binary",))
        self.page.on("dialog", lambda d: d.accept())
        self.page.click("#nfRepairBtn")
        self.advance(1000, step=250)
        self.assertEqual(sent, [{"port": 2586, "bind": "0.0.0.0", "topic": "example-topic"}])
        self.assertEqual(self.page.evaluate("getAt(workingConfig, 'notifications.url')"),
                         "http://localhost:2586/example-topic")
        self.assertIn("ntfy repaired", self.text(".alert-modal"))


class TestStatusNotificationsCard(BrowserCase):
    """The Status page says whether alerts can reach the phone: off, working,
    the ntfy server not answering, or the last alert refused."""

    def card(self, notifications, **extra):
        self.site.api["/api/status"] = _status_page(notifications, **extra)
        self.open("/status")
        self.advance(1000)
        self.assertNotIn("Failed to load status", self.page.inner_text("body"))
        if self.page.locator("#notificationsCard").count() == 0:
            return None
        return self.text("#notificationsCard")

    def test_off(self):
        text = self.card({"enabled": False, "ok": True})
        self.assertIn("Off", text)

    def test_working(self):
        text = self.card({"enabled": True, "ok": True, "reachable": True, "server": "127.0.0.1:2586",
                          "response_ms": 4, "error": None, "last_sent_ts": 1781539200,
                          "last_failed": None})
        self.assertIn("Reachable", text)
        self.assertIn("127.0.0.1:2586", text)
        self.assertNotIn("Can't reach", text)

    def test_unreachable_says_so_and_names_it_in_the_banner(self):
        text = self.card({"enabled": True, "ok": False, "reachable": False, "server": "127.0.0.1:2586",
                          "response_ms": None, "error": "couldn't connect", "last_sent_ts": None,
                          "last_failed": None},
                         severity="warn", failing=["notifications"],
                         health_reason="notification server unreachable — alerts aren't reaching your phone")
        self.assertIn("Unreachable", text)
        self.assertIn("Can't reach the notification server (couldn't connect)", text)
        self.assertEqual(self.page.get_attribute("#notificationsCard a", "href"), "/config#notifications")
        self.assertIn("notifications", self.page.inner_text(".overall"))

    def test_the_last_alert_refused(self):
        text = self.card({"enabled": True, "ok": False, "reachable": True, "server": "127.0.0.1:2586",
                          "response_ms": 4, "error": None, "last_sent_ts": 1781539200,
                          "last_failed": {"ts": 1781542800, "event": "watchlist", "error": "HTTP 403"}},
                         severity="warn", failing=["notifications"])
        self.assertIn("Last alert failed", text)
        self.assertIn("failed: HTTP 403", text)

    def test_an_older_server_without_the_component_draws_no_card(self):
        self.assertIsNone(self.card(None))


class TestHealthDotMatchesAcrossPages(BrowserCase):

    def dot(self, path, status):
        self.site.api["/api/status"] = status
        self.site.api["/api/live"] = _aircraft(1)
        self.open(path)
        self.advance(1000)
        cls = self.page.get_attribute("#healthDot", "class") or ""
        return "error" if "off" in cls else "warn" if "warn" in cls else "ok"

    def test_same_colour_for_each_severity(self):
        cases = {
            "warn": _status(severity="warn", failing=["capacity"], health_reason="disk capacity"),
            "error": _status(severity="error", failing=["receiver"], health_reason="receiver offline",
                             components={"receiver": {"ok": False}}),
            "ok": _status(system={"load_average": {"15m": 99}, "cpu_cores": 1},
                          sudoers={"can_verify": True, "needs_refresh": True,
                                   "expected_version": 6, "live_version": 5}),
        }
        for want, status in cases.items():
            for path in ("/", "/status"):
                with self.subTest(severity=want, page=path):
                    self.assertEqual(self.dot(path, status), want)

    def test_an_update_lights_both(self):
        for path in ("/", "/status"):
            with self.subTest(page=path):
                self.assertEqual(self.dot(path, _status(update_available=True)), "warn")

    def test_another_page_turns_red_when_the_server_is_gone(self):
        self.dot("/status", _status())
        self.site.down = True
        self.advance(65000, step=5000)
        self.assertIn("off", self.page.get_attribute("#healthDot", "class"))
        self.site.down = False
        self.advance(35000, step=5000)
        self.assertNotIn("off", self.page.get_attribute("#healthDot", "class"))


def _rows(*icaos):
    return [{"icao": i, "callsign": f"CS{i}", "aircraft_type": "B738", "sighting_count": 1,
             "first_seen_at": 1700000000, "last_seen_at": 1700000000} for i in icaos]


class TestStatusAskedForOncePerPage(BrowserCase):
    """Every /api/status call makes the server probe the receiver, so a page asks
    once per 30 s cycle however many features on it want the answer. The shared
    demo banner rides on the page's own poll instead of running a second one."""

    def banner(self):
        return self.page.evaluate(
            "() => { var e = document.getElementById('demoBanner');"
            " return e ? e.style.display : 'missing'; }")

    def ask(self, path, seconds, limit):
        self.site.api["/api/status"] = _status()
        self.site.api["/api/live"] = _aircraft(1)
        self.open(path)
        self.advance(1000)
        asked = self.site.count("/api/status")
        self.advance(seconds * 1000)
        self.assertLessEqual(self.site.count("/api/status") - asked, limit,
                             "asked: " + str(self.site.count("/api/status") - asked))

    def test_the_updates_page_asks_once_per_cycle(self):
        # Only health-indicator.js's poll; the banner used to add its own.
        self.ask("/updates", 60, 2)

    def test_index_asks_once_per_cycle(self):
        # Only _pollSystemStatus; the banner used to add its own.
        self.ask("/", 60, 3)

    def test_the_demo_banner_rides_the_pages_poll(self):
        self.site.api["/api/status"] = _status(demo_enabled=True)
        self.open("/updates")
        self.advance(1000)
        self.assertEqual(self.banner(), "block")
        self.site.api["/api/status"] = _status(demo_enabled=False)
        asked = self.site.count("/api/status")
        self.advance(31000)
        self.assertEqual(self.banner(), "none")
        self.assertLessEqual(self.site.count("/api/status") - asked, 1,
                             "the flip came from the page's poll, not a second request")


class TestSearchRaces(BrowserCase):

    def setUp(self):
        super().setUp()
        self.site.api["/api/search"] = self.answer
        self.slow = set()
        self.pages = {"alpha": [_rows("AAA001", "AAA002"), _rows("AAA003", "AAA004")],
                      "bravo": [_rows("BBB001", "BBB002")]}
        self.site.api["/api/live"] = _aircraft(1)
        self.site.api["/api/status"] = _status()
        self.open("/")
        self.page.evaluate("go('search')")
        self.page.wait_for_timeout(50)

    def answer(self, req, hold=True):
        qs = parse_qs(urlparse(req.url).query)
        q = qs["q"][0]
        offset = int(qs.get("offset", ["0"])[0])
        if hold and (q, offset) in self.slow:
            return HOLD
        pages = self.pages[q]
        limit = len(pages[0])
        return {"ok": True, "rows": pages[offset // limit] if offset // limit < len(pages) else [],
                "total_count": sum(map(len, pages)), "parsed_filters": [], "free_text": [q],
                "time_range": None, "time_label": None, "time_tokens": [], "execution_ms": 1}

    def release(self):
        self.site.release(lambda req: self.answer(req, hold=False))
        self.page.wait_for_timeout(100)

    def search(self, q):
        self.page.evaluate("""q => { document.getElementById('searchInput').value = q; runSearch(); }""", q)
        self.page.wait_for_timeout(100)

    def icaos(self):
        return self.page.evaluate("_searchState.rows.map(r => r.icao)")

    def test_a_late_answer_for_an_old_search_is_ignored(self):
        self.slow.add(("alpha", 0))
        self.search("alpha")
        self.search("bravo")
        self.assertEqual(self.icaos(), ["BBB001", "BBB002"])
        self.release()
        self.assertEqual(self.icaos(), ["BBB001", "BBB002"])
        self.assertIn("BBB001", self.text("#searchResults"))
        self.assertNotIn("AAA001", self.text("#searchResults"))

    def test_clearing_the_box_stays_cleared(self):
        self.slow.add(("alpha", 0))
        self.search("alpha")
        self.search("")
        self.release()
        self.assertEqual(self.text("#searchResults").strip(), "")

    def test_double_clicking_load_more_adds_each_row_once(self):
        self.search("alpha")
        self.slow.add(("alpha", 2))
        self.page.dblclick("#searchLoadMoreBtn")
        self.page.wait_for_timeout(100)
        self.release()
        self.assertEqual(self.icaos(), ["AAA001", "AAA002", "AAA003", "AAA004"])

    def test_load_more_from_the_old_search_cant_join_the_new_one(self):
        self.search("alpha")
        self.slow.add(("alpha", 2))
        self.page.click("#searchLoadMoreBtn")
        self.page.wait_for_timeout(50)
        self.search("bravo")
        self.release()
        self.assertEqual(self.icaos(), ["BBB001", "BBB002"])

    def test_load_more_works_again_after_a_failure(self):
        self.search("alpha")
        self.site.api["/api/search"] = (500, "boom")
        self.page.click("#searchLoadMoreBtn")
        self.page.wait_for_timeout(100)
        self.site.api["/api/search"] = self.answer
        self.page.evaluate("loadMoreSearch()")
        self.page.wait_for_timeout(100)
        self.assertEqual(self.icaos(), ["AAA001", "AAA002", "AAA003", "AAA004"])


class TestTodayButton(BrowserCase):
    # The browser is in Tokyo; only the server knows Aerodrome's timezone.
    TIMEZONE = "Asia/Tokyo"

    def test_today_is_worked_out_by_the_server(self):
        self.site.api["/api/search"] = {"ok": True, "rows": [], "total_count": 0}
        self.site.api["/api/live"] = _aircraft(1)
        self.site.api["/api/status"] = _status()
        self.open("/")
        self.page.evaluate("go('search')")
        self.page.evaluate("document.getElementById('searchInput').value = 'B738'; runSearch()")
        self.page.wait_for_timeout(100)
        self.page.evaluate("srchPreset('today')")
        self.page.wait_for_timeout(100)
        _, qs = [c for c in self.site.calls if c[0] == "/api/search"][-1]
        self.assertEqual(qs.get("preset"), ["today"])
        self.assertNotIn("from_ts", qs)


class TestAircraftPageRaces(BrowserCase):
    ROWS = {"seen_at": [1, 2, 3], "altitude": [3, 1, 2]}

    def setUp(self):
        super().setUp()
        self.hold_sightings = False
        self.hold_window = None
        self.site.api["/api/aircraft/A00001"] = {
            "icao": "A00001", "first_seen_at": 1700000000, "last_seen_at": 1700000000,
            "sighting_count": 3, "recent_sightings": [], "callsigns": []}
        self.site.api["/api/all/drill"] = self.sightings
        self.site.api["/api/aircraft/A00001/positions"] = self.positions
        self.site.api["/api/live"] = _aircraft(1)
        self.site.api["/api/status"] = _status()

    def sightings(self, req, hold=True):
        if hold and self.hold_sightings:
            return HOLD
        qs = parse_qs(urlparse(req.url).query)
        order = qs["order"][0]
        limit = int(qs["limit"][0])
        ids = self.ROWS.get(order, self.ROWS["seen_at"])
        return {"ok": True, "total_count": 3, "sightings": [
            {"seen_at": 1700000000 + i, "callsign": f"O{order}", "altitude": i * 100,
             "speed": 1, "lat": 40, "lon": -90} for i in ids[:limit]]}

    def positions(self, req, hold=True):
        window = parse_qs(urlparse(req.url).query)["window"][0]
        if hold and window == self.hold_window:
            return HOLD
        n = {"24h": 2, "7d": 5}.get(window, 1)
        return {"ok": True, "window": window, "receiver": {"lat": 40, "lon": -90, "distance_unit": "mi"},
                "count": n, "truncated": False,
                "positions": [[1700000000 + i, 40 + i / 100, -90, 12000] for i in range(n)]}

    def test_sorting_while_a_page_loads_still_sorts(self):
        self.hold_sightings = True
        self.open("/aircraft/A00001")
        self.hold_sightings = False
        self.page.evaluate("srtSightings('altitude')")
        self.page.wait_for_timeout(100)
        self.site.release(lambda req: self.sightings(req, hold=False))
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.evaluate("_sightingsState.rows[0].callsign"), "Oaltitude")

    def test_changing_page_size_while_a_page_loads_still_applies(self):
        self.hold_sightings = True
        self.open("/aircraft/A00001")
        self.hold_sightings = False
        self.page.evaluate("""() => {
            const s = document.getElementById('sightingsPageSize');
            const o = document.createElement('option'); o.value = '2'; s.appendChild(o);
            s.value = '2'; onSightingsPageSizeChange(); }""")
        self.page.wait_for_timeout(100)
        self.site.release(lambda req: self.sightings(req, hold=False))
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.evaluate("_sightingsState.rows.length"), 2)

    def test_double_load_more_is_one_page(self):
        self.open("/aircraft/A00001")
        self.page.evaluate("() => { _sightingsState.pageSize = 2; _sightingsState.offset = 0; _sightingsState.rows = []; }")
        self.page.evaluate("fetchSightings()")
        self.page.wait_for_timeout(100)
        self.hold_sightings = True
        self.page.evaluate("loadMoreSightings(); loadMoreSightings();")
        self.page.wait_for_timeout(100)
        self.assertEqual(len(self.site.held), 1)

    def test_a_late_trail_for_the_old_window_is_ignored(self):
        self.open("/aircraft/A00001")
        self.page.wait_for_timeout(200)
        self.hold_window = "30d"
        self.page.click(".win-btn[data-win='30d']")
        self.page.wait_for_timeout(100)
        self.page.click(".win-btn[data-win='7d']")
        self.page.wait_for_timeout(150)
        shown = self.text("#mapMeta")
        self.site.release(lambda req: self.positions(req, hold=False))
        self.page.wait_for_timeout(150)
        self.assertEqual(self.text("#mapMeta"), shown)
        self.assertRegex(shown, r"\b5\b")


class TestStatsDrillSurvivesRefresh(BrowserCase):
    STATS = {"enabled": True, "timezone": "UTC", "cards": {"unique_today": 5},
             "groups": [{"id": "g", "label": "G", "cards": ["unique_today"]}]}
    DRILL = {"card": "unique_today", "total_count": 1, "count": 1, "rows": [
        {"icao": "A00001", "callsign": "DEMO1", "aircraft_type": "B738", "hits": 3,
         "value": 3, "first_seen_at": 1700000000, "last_seen_at": 1700000000}]}

    def setUp(self):
        super().setUp()
        self.site.api["/api/stats"] = self.STATS
        self.site.api["/api/stats/drill"] = self.DRILL
        self.site.api["/api/live"] = _aircraft(1)
        self.site.api["/api/status"] = _status()
        self.open("/")
        self.page.evaluate("go('stats')")
        self.page.wait_for_selector(".stat-card.drillable")

    def refresh(self):
        self.page.evaluate("fetchStats()")
        self.page.wait_for_timeout(150)

    def test_an_open_drill_stays_open(self):
        self.page.click(".stat-card.drillable")
        self.page.wait_for_selector(".drill-panel-row .drill-grid")
        self.page.evaluate("document.querySelector('.drill-panel-row').dataset.mark = 'same'")
        self.refresh()
        self.assertEqual(self.page.evaluate(
            "document.querySelector('.drill-panel-row')?.dataset.mark"), "same")
        self.assertEqual(self.page.locator(".stat-card.drill-source").count(), 1)
        self.page.keyboard.press("Escape")
        self.assertEqual(self.page.locator(".drill-panel-row").count(), 0)

    def test_a_drill_still_loading_fills_in(self):
        self.site.api["/api/stats/drill"] = lambda req: HOLD
        self.page.click(".stat-card.drillable")
        self.page.wait_for_selector(".drill-panel-row")
        self.refresh()
        self.site.release(self.DRILL)
        self.page.wait_for_selector(".drill-panel-row .drill-grid", timeout=3000)
        self.assertEqual(self.page.locator(".drill-panel-row .drill-loading").count(), 0)

    def test_a_card_thats_gone_takes_its_drill_with_it(self):
        self.page.click(".stat-card.drillable")
        self.page.wait_for_selector(".drill-panel-row .drill-grid")
        self.site.api["/api/stats"] = dict(self.STATS, cards={"military_today": 1},
                                           groups=[{"id": "g", "label": "G", "cards": ["military_today"]}])
        self.refresh()
        self.assertEqual(self.page.locator(".drill-panel-row").count(), 0)
        self.assertEqual(self.errors, [])


class TestWatchlistAlertInLiveMode(BrowserCase):
    """Trigger "live" asks /api/live's is_watchlist mark — the server's full
    matcher — not the page's hex-only wlIcaos set, so callsign and model
    entries light the tab too. Hexes and callsigns invented."""

    UNWATCHED = {"icao": "A00009", "callsign": "DEMO9", "altitude": 12000,
                 "speed": 300, "lat": 40.1, "lon": -90.1, "track": 90,
                 "is_watchlist": False}
    WATCHED = {"icao": "A00001", "callsign": "TEST123", "aircraft_type": "DASH8",
               "altitude": 8000, "speed": 240, "lat": 40.2, "lon": -90.2,
               "track": 45, "is_watchlist": True}

    def setUp(self):
        super().setUp()
        self.site.api["/api/ui-config"] = {"watchlist_alerts": {
            "enabled": True, "trigger": "live", "effect": "pulse_dot",
            "color": "#f59e0b"}}
        # A callsign entry and a model entry: wlIcaos gets no hexes from these.
        self.site.api["/api/watchlist/entries"] = {"entries": [
            {"callsign": "TEST123", "label": "Test flight"},
            {"model": "DASH8", "label": "Test model"}]}
        self.site.api["/api/watchlist"] = {"aircraft": []}
        self.site.api["/api/military"] = {"aircraft": [], "special_aircraft": {}}
        self.site.api["/api/status"] = _status()
        self.set_live(self.UNWATCHED)
        self.open("/")
        self.advance(1000)

    def set_live(self, *rows):
        self.site.api["/api/live"] = {
            "aircraft": [dict(r) for r in rows],
            "receiver": {"lat": 40.0, "lon": -90.0, "distance_unit": "mi"},
            "last_updated": 1700000000}

    def poll(self):
        self.page.evaluate("fetchLive()")
        self.page.wait_for_timeout(150)

    def alert_class(self):
        cls = self.page.get_attribute('.tab[data-t="watchlist"]', "class") or ""
        return next((c for c in cls.split() if c.startswith("wa-")), None)

    def test_a_watchlist_row_lights_the_tab_even_with_no_matching_hex(self):
        # A00001 is on no watchlist entry by hex or tail — the mark came from
        # the callsign/model matcher on the server.
        self.set_live(self.WATCHED, self.UNWATCHED)
        self.poll()
        self.assertEqual(self.alert_class(), "wa-pulse-dot")

    def test_rows_nobody_watches_leave_the_tab_quiet(self):
        self.set_live(self.UNWATCHED, dict(self.UNWATCHED, icao="A00010"))
        self.poll()
        self.assertIsNone(self.alert_class())

    def test_the_alert_clears_when_the_watched_aircraft_is_gone(self):
        self.set_live(self.WATCHED)
        self.poll()
        self.assertEqual(self.alert_class(), "wa-pulse-dot")
        self.set_live(self.UNWATCHED)
        self.poll()
        self.assertIsNone(self.alert_class())


class TestNoPageErrors(BrowserCase):

    def test_pages_load_clean(self):
        self.site.api["/api/live"] = _aircraft(2)
        self.site.api["/api/status"] = _status()
        for path in ("/", "/board?mode=board", "/board?mode=radar", "/board?mode=hybrid", "/status"):
            with self.subTest(page=path):
                self.errors.clear()
                self.open(path)
                self.advance(6000)
                self.assertEqual(self.errors, [])


class TestUpdatesApplyWaitsForARestore(BrowserCase):
    # /api/maintenance/status reports restore.pending while a restore waits
    # for its restart; the server answers 409 restore_pending to an apply,
    # so the page must not offer one meanwhile.
    RESTORE = {"busy": None, "update": None, "update_pending": None, "config": None,
               "restore": {"pending": {"staged_at": 1700000000, "expires_at": 1700003600,
                                       "restart_failed": False, "waiting_for_restart": True,
                                       "source": "example-backup.zip",
                                       "backup_created_at_iso": "2026-01-01T12:00:00+00:00",
                                       "keep_current_config": False, "notes": "",
                                       "kind": "restore"},
                           "result": None}}
    IDLE = {"busy": None, "update": None, "update_pending": None, "config": None,
            "restore": {"pending": None, "result": None}}
    STAGED = {"available": True, "current_version": "3.4.150", "update_version": "3.4.151",
              "install_dir": "/opt/aerodrome",
              "sudoers": {"can_verify": True, "needs_refresh": False}}

    def setUp(self):
        super().setUp()
        self.site.api["/api/version"] = {"version": "3.4.150"}
        self.site.api["/api/updates/local/check"] = self.STAGED
        self.open("/updates")
        self.advance(1000)

    def test_a_waiting_restore_disables_apply_and_says_why(self):
        self.site.api["/api/maintenance/status"] = self.RESTORE
        self.page.evaluate("refreshMaintenance()")
        self.page.wait_for_timeout(100)
        self.assertTrue(self.page.evaluate("document.getElementById('btnApply').disabled"))
        self.assertIn("restart Aerodrome", self.text("#maintBusy"))

    def test_apply_is_free_again_once_nothing_waits(self):
        self.site.api["/api/maintenance/status"] = self.RESTORE
        self.page.evaluate("refreshMaintenance()")
        self.page.wait_for_timeout(100)
        self.site.api["/api/maintenance/status"] = self.IDLE
        self.page.evaluate("refreshMaintenance()")
        self.page.wait_for_timeout(100)
        self.assertFalse(self.page.evaluate("document.getElementById('btnApply').disabled"))
        self.assertEqual(self.page.locator("#maintBusy").count(), 0)


class TestUpdatesLocalNothingStaged(BrowserCase):
    # The Local update card only knows what's in update/, so with nothing there
    # it says so instead of claiming the install is up to date.
    def setUp(self):
        super().setUp()
        self.site.api["/api/version"] = {"version": "3.4.150"}
        self.site.api["/api/sudoers/status"] = {"can_verify": True, "needs_refresh": False}
        self.site.api["/api/maintenance/status"] = {
            "busy": None, "update": None, "update_pending": None, "config": None,
            "restore": {"pending": None, "result": None}}

    def card(self, staged, reason):
        self.site.api["/api/updates/local/check"] = {
            "available": False, "current_version": "3.4.150", "update_version": None,
            "staged": staged, "reason": reason,
            "preflight": {"ok": True, "problems": [], "message": None, "fix_command": None}}
        self.open("/updates")
        self.advance(1000)
        return self.text("#localBadge").strip().lower(), self.text("#localStatus")

    def test_nothing_staged(self):
        badge, note = self.card("none", "Nothing staged. To update, upload a release zip below, "
                                        "or copy one into update/ over SSH.")
        self.assertEqual(badge, "nothing staged")
        self.assertIn("Nothing staged.", note)

    def test_a_partial_copy_keeps_its_note(self):
        badge, note = self.card("partial", "No VERSION file in update/. "
                                           "Make sure you copied the full release contents.")
        self.assertEqual(badge, "up to date")
        self.assertIn("No VERSION file", note)


class TestUpdatesGithubCannotReadTheTag(BrowserCase):
    # A stored tag parse_version can't parse is not evidence of being up to
    # date — the card says it can't tell instead of claiming it is.
    def setUp(self):
        super().setUp()
        self.site.api["/api/version"] = {"version": "3.4.150"}
        self.site.api["/api/updates/local/check"] = {"available": False,
                                                     "current_version": "3.4.150"}
        self.site.api["/api/maintenance/status"] = {
            "busy": None, "update": None, "update_pending": None, "config": None,
            "restore": {"pending": None, "result": None}}

    def card_text(self, latest, unreadable):
        self.site.api["/api/updates/github/check"] = {
            "enabled": True, "poll_interval": "monthly", "running_version": "3.4.150",
            "latest_known": latest, "available": False, "latest_unreadable": unreadable,
            "last_check_ts": 1700000000, "last_check_result": "success",
            "last_check_error": None, "last_known_latest_ts": 1700000000,
            "release_url": None}
        self.open("/updates")
        self.advance(1000)
        return self.text("#githubUpdateStatus")

    def test_an_unreadable_tag_says_the_truth(self):
        t = self.card_text("v.3.4.53", True)
        self.assertIn("can't read", t)
        self.assertNotIn("latest version", t)

    def test_a_readable_older_tag_still_says_latest(self):
        self.assertIn("latest version", self.card_text("v3.4.53", False))


_CONTRAST_JS = """(sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const rgba = (s) => {
        const m = s.match(/([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?/);
        return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null;
    };
    const chan = (c) => { c /= 255;
        return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    const lum = (c) => 0.2126 * chan(c[0]) + 0.7152 * chan(c[1]) + 0.0722 * chan(c[2]);
    const fg = rgba(getComputedStyle(el).color);
    let bg = null;
    for (let n = el; n && !bg; n = n.parentElement) {
        const c = rgba(getComputedStyle(n).backgroundColor);
        if (c && c[3] >= 1) bg = c;
    }
    if (!fg || !bg) return null;
    const a = lum(fg), b = lum(bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}"""


class TestTextContrast(BrowserCase):
    """WCAG AA (4.5:1) for themed text, measured in the browser from live
    computed styles: each element's `color` against the first opaque
    `background-color` found by walking up from it."""

    AA = 4.5

    def themed(self, theme):
        """A fresh /config page with `theme` saved and the OS scheme matching."""
        self.context.close()
        self.context = self.browser.new_context(
            timezone_id=self.TIMEZONE, service_workers="block", color_scheme=theme)
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.site = Site(self.page)
        self.page.clock.install(time=self.START)
        self.page.add_init_script(f"localStorage.setItem('aerodrome-theme', '{theme}')")
        self.open("/config")

    def assertAA(self, selector, theme, what):
        r = self.page.evaluate(_CONTRAST_JS, selector)
        self.assertIsNotNone(r, f"nothing on the page matches {selector}")
        self.assertGreaterEqual(r, self.AA,
                                f"{what} in {theme} theme: {r:.2f}:1 < {self.AA}:1")

    def test_alert_modal_titles_and_ok_button_meet_aa_in_both_themes(self):
        for theme in ("light", "dark"):
            self.themed(theme)
            for kind in ("error", "warning", "success", "info"):
                with self.subTest(theme=theme, kind=kind):
                    # A dismissed dialog is a resolved promise; dismiss any a
                    # failed subTest left open so each kind measures its own.
                    self.page.evaluate(
                        "() => document.querySelectorAll('.alert-modal-ok')"
                        ".forEach(b => b.click())")
                    # Braces so the arrow returns undefined, not the dialog's
                    # promise — that one only resolves when the user dismisses.
                    self.page.evaluate(f"() => {{ alertModal('Title', 'Body', '{kind}'); }}")
                    self.assertAA(".alert-modal .modal-title", theme, f"{kind} dialog title")
                    self.assertAA(".alert-modal .alert-modal-ok", theme, f"{kind} OK button")
                    self.page.click(".alert-modal-ok")

    def test_copy_feedback_text_meets_aa_in_both_themes(self):
        for theme in ("light", "dark"):
            self.themed(theme)
            self.page.evaluate("""() => {
                try {
                    Object.defineProperty(navigator, 'clipboard',
                                          {value: undefined, configurable: true});
                } catch (e) {}
                const btn = document.createElement('button');
                btn.className = 'btn'; btn.id = 'contrastCopyBtn';
                btn.textContent = 'Copy';
                document.body.appendChild(btn);
            }""")
            for copied, label in ((True, "✓ copied!"), (False, "✗ blocked")):
                with self.subTest(theme=theme, result=label):
                    self.page.evaluate("ok => { document.execCommand = () => ok; }", copied)
                    self.page.evaluate("""() => {
                        copyToClipboard('x', document.getElementById('contrastCopyBtn'));
                    }""")
                    self.page.wait_for_timeout(250)   # the 150ms color transition settles
                    self.assertEqual(self.text("#contrastCopyBtn"), label)
                    self.assertAA("#contrastCopyBtn", theme, f"copy feedback {label!r}")

    def test_light_primary_button_meets_aa(self):
        self.themed("light")
        self.page.evaluate("""() => {
            const btn = document.createElement('button');
            btn.className = 'btn btn-primary'; btn.id = 'contrastPrimaryBtn';
            btn.textContent = 'Save';
            document.body.appendChild(btn);
        }""")
        self.assertAA("#contrastPrimaryBtn", "light", "primary button")


if __name__ == "__main__":
    unittest.main()
