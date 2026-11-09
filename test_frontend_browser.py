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
         "/updates": "updates.html", "/config": "config.html",
         "/performance": "performance.html"}


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


def _perf(table_sizes=None, size_bytes=2_800_000_000):
    """A /api/perf/diagnostics answer. Sizes are only in it when the run
    measured them (v3.4.171); the database is a 2.6 GB one, which is what the
    page's estimate of the cost is worked out from."""
    return {"ok": True, "generated_at": 1781539200, "aerodrome_version": "0.0.0",
            "sqlite_version": "3.46.1", "python_version": "3.12.0",
            "platform": {"system": "Linux", "machine": "x86_64", "processor": "x86_64",
                         "release": "6.6.0"},
            "storage": {"db_file": "/opt/aerodrome/data/history.db",
                        "size_bytes": size_bytes, "size_human": "2.6 GB"},
            "table_sizes": table_sizes,
            "tables": [{"name": "all_sightings", "rows": 1200, "count_ms": 4.0,
                        "size_bytes": 2_411_724 if table_sizes else None},
                       {"name": "seen_aircraft", "rows": 40, "count_ms": 0.4}],
            "indexes": [{"name": "idx_all_seen_icao", "table": "all_sightings"}],
            "queries": [], "hints": [], "pragmas": {}, "io_baseline": {},
            "include_legacy": False, "legacy_probes_skipped": 1}


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


class TestFlightAwareSettings(_ConfigPage):
    """Configuration → Display → FlightAware routes (v3.4.168): sign-up steps
    without a key; a saved key shows as its last 4 characters only, with this
    month's lookups; Replace and Remove only change the unsaved copy."""

    STATUS = {"ok": True, "configured": True, "key_last4": "7c2e", "key_state": "ok", "tested_at": 1,
              "month": "2026-11", "count": 212, "limit": 900, "paused": False, "resets_on": "2026-12-01",
              "cost_estimate": 1.06, "limit_cost": 4.5,
              "usage": {"total_cost": 0.95, "total_discount_cost": 0.0, "total_calls": 190, "as_of": 1},
              "usage_url": "https://flightaware.example/usage", "signup_url": "https://flightaware.example/"}

    def open_with(self, key, status=None):
        cfg = json.loads(json.dumps(_CONFIG))
        cfg["display"] = dict(cfg.get("display") or {}, show_routes=True)
        cfg["flightaware"] = {"api_key": key, "monthly_limit": 900, "watchlist_lookups": False}
        self.site.api["/api/flightaware/status"] = status or self.STATUS
        self.page.set_viewport_size({"width": self.WIDTH, "height": 900})
        self.site.api["/api/config"] = cfg
        self.site.api["/api/status"] = _status()
        self.open("/config")
        self.advance(1000)
        self.tab("display")
        self.advance(500, step=250)

    def box(self):
        return self.text("#faBox")

    def test_opening_the_tab_changes_nothing(self):
        for key in ("", "__kept__"):
            self.open_with(key)
            self.assertTrue(self.page.evaluate("deepEqual(originalConfig, workingConfig)"), key)
            self.assertFalse(self.page.evaluate("tabIsDirty('display')"), key)

    def test_no_key_shows_the_steps_and_asks_for_nothing(self):
        self.open_with("")
        self.assertIn("Sign up for AeroAPI Personal", self.box())
        self.assertIn("personal use only", self.box())
        self.assertEqual(self.site.count("/api/flightaware/status"), 0)
        self.assertEqual(self.page.evaluate("document.getElementById('faKeyInput').type"), "password")

    def test_a_saved_key_shows_only_its_last_4_and_the_months_lookups(self):
        self.open_with("__kept__")
        box = self.box()
        self.assertIn("•••• •••• •••• 7c2e", box)
        self.assertIn("212 of 900 lookups this month", box)
        self.assertIn("$0.95 so far, $0.00 after your free credit", box)
        self.assertIn("connected", box)
        self.assertIsNone(self.page.query_selector("#faKeyInput"))

    def test_the_limit_reached_state(self):
        self.open_with("__kept__", dict(self.STATUS, count=900, paused=True, cost_estimate=4.5))
        self.assertIn("This month's budget is used up", self.box())
        self.assertIn("paused until Dec", self.box())

    def test_the_budget_is_typed_in_dollars_and_stored_as_lookups(self):
        self.open_with("__kept__")
        self.assertEqual(self.page.input_value("#faBudget"), "4.50")
        self.assertIn("about 900 lookups", self.text("#faBudgetNote"))
        self.page.fill("#faBudget", "9.50")
        self.assertEqual(self.page.evaluate("workingConfig.flightaware.monthly_limit"), 1900)
        self.assertIn("about 1,900 lookups", self.text("#faBudgetNote"))
        self.assertTrue(self.page.evaluate("tabIsDirty('display')"))
        self.page.fill("#faBudget", "0")
        self.assertEqual(self.page.evaluate("workingConfig.flightaware.monthly_limit"), 0)
        self.page.fill("#faBudget", "")
        self.assertIsNone(self.page.evaluate("workingConfig.flightaware.monthly_limit"))

    def test_remove_and_replace_change_only_the_unsaved_copy(self):
        self.open_with("__kept__")
        self.page.click("#faBox button:has-text('Remove')")
        self.advance(300, step=150)
        self.assertEqual(self.page.evaluate("workingConfig.flightaware.api_key"), "")
        self.assertIn("removed when you save", self.box())
        self.assertTrue(self.page.evaluate("tabIsDirty('display')"))
        self.page.evaluate("faCancelReplace()")
        self.advance(300, step=150)
        self.assertEqual(self.page.evaluate("workingConfig.flightaware.api_key"), "__kept__")
        self.page.click("#faBox button:has-text('Replace')")
        self.advance(300, step=150)
        self.page.fill("#faKeyInput", "EXAMPLEkey1111111111111111111111")
        self.assertEqual(self.page.evaluate("workingConfig.flightaware.api_key"), "EXAMPLEkey1111111111111111111111")
        self.assertEqual(self.page.evaluate("tabForPath('flightaware.api_key')"), "display")

    def test_test_key_reports_the_result(self):
        self.open_with("__kept__")
        self.site.api["/api/flightaware/test"] = {"ok": False, "state": "invalid",
                                                  "message": "FlightAware didn't accept this key."}
        self.page.click("#faTestBtn")
        self.advance(600, step=200)
        self.assertIn("FlightAware didn't accept this key.", self.box())
        self.assertGreaterEqual(self.site.count("/api/flightaware/status"), 2, "status re-read after the test")


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


class TestStatusFlightAwareCard(BrowserCase):
    """v3.4.169: with a FlightAware key, the Status page shows this month's
    lookups and budget, FlightAware's own figure and the last lookup; a refused
    key is amber there and on the Status dot, a used-up budget on the card only."""

    FA = {"configured": True, "key_last4": "7c2e", "key_state": "ok", "tested_at": 1, "month": "2026-11",
          "count": 212, "key_count": 212, "limit": 900, "paused": False, "resets_on": "2026-12-01",
          "cost_estimate": 1.06, "limit_cost": 4.5,
          "usage": {"total_cost": 0.95, "total_discount_cost": 0.0, "total_calls": 190, "lookups": 190,
                    "as_of": 4102444800,
                    "account": {"total_cost": 0.95, "total_discount_cost": 0.0, "total_calls": 190, "lookups": 190}},
          "usage_url": "https://flightaware.example/usage",
          "last_lookup": {"ts": 1781539200, "callsign": "EXA412", "status": "found",
                          "origin": "KBOS", "destination": "KDEN"}}

    def card(self, fa, **extra):
        st = _status_page({"enabled": False, "ok": True}, **extra)
        st["components"]["flightaware"] = fa
        self.site.api["/api/status"] = st
        self.site.api["/api/flightaware/status"] = dict(fa or {}, ok=True)
        self.open("/status")
        self.advance(1000)
        self.assertNotIn("Failed to load status", self.page.inner_text("body"))
        if self.page.locator("#flightawareCard").count() == 0:
            return None
        return self.text("#flightawareCard")

    def test_no_key_no_card(self):
        self.assertIsNone(self.card(None))
        self.assertIsNone(self.card({"configured": False}))

    def test_connected(self):
        text = self.card(self.FA)
        for want in ("Connected", "This Aerodrome", "212 of 900 lookups", "$1.06 of $4.50 budget",
                     "This key", "…7c2e, in use", "190 · $0.95", "FlightAware as of", "after the free credit",
                     "EXA412 · KBOS → KDEN", "December 1", "key …7c2e"):
            self.assertIn(want, text)
        self.assertNotIn("Whole account", text, "one key: the account row would only repeat it")
        self.assertNotIn("earlier key", text)
        self.assertEqual(self.page.get_attribute("#flightawareCard .fa-links a >> nth=1", "href"), "/config#display")
        self.assertEqual(self.site.count("/api/flightaware/status"), 0, "a fresh figure isn't fetched again")

    def test_lookups_with_this_key_made_elsewhere_get_a_note(self):
        text = self.card(dict(self.FA, usage=dict(self.FA["usage"], lookups=300)))
        self.assertIn("88 lookups with key …7c2e weren't made by this Aerodrome", text)

    def test_two_keys_show_both_figures_and_the_split(self):
        # v3.4.175: the owner changed keys this month; the count spans both.
        usage = dict(self.FA["usage"], lookups=3, total_cost=0.01,
                     account={"total_cost": 0.53, "total_discount_cost": 0.0, "total_calls": 128, "lookups": 105})
        text = self.card(dict(self.FA, count=28, key_count=3, usage=usage))
        for want in ("28 of 900 lookups", "This key", "3 · $0.01", "Other keys", "an earlier key",
                     "102 · $0.52", "Whole account", "105 · $0.53",
                     "77 lookups on other keys weren't made by this Aerodrome"):
            self.assertIn(want, text)
        cells = self.page.eval_on_selector_all("#flightawareCard .fa-keys .v", "els => els.map(e => e.textContent)")
        self.assertEqual(cells, ["3", "3 · $0.01", "25", "102 · $0.52", "28", "105 · $0.53"])

    def test_earlier_keys_are_named_by_their_last_4(self):
        # v3.4.176: a key replaced after the update is remembered by its last 4.
        usage = dict(self.FA["usage"], lookups=10, total_cost=0.05,
                     account={"total_cost": 0.74, "total_discount_cost": 0.0, "total_calls": 160, "lookups": 147})
        text = self.card(dict(self.FA, count=40, key_count=10, key_last4="d07a",
                              earlier_keys=[{"last4": "9b41", "count": 30}], usage=usage))
        for want in ("…d07a, in use", "earlier: …9b41", "137 · $0.69", "147 · $0.74"):
            self.assertIn(want, text)

    def test_an_unknown_key_count_claims_nothing(self):
        text = self.card(dict(self.FA, key_count=None, usage=dict(self.FA["usage"], lookups=300)))
        self.assertNotIn("earlier key", text)
        self.assertEqual(self.page.locator("#flightawareCard .fa-note").count(), 0)

    def test_no_note_when_this_install_covers_the_account(self):
        self.card(self.FA)
        self.assertEqual(self.page.locator("#flightawareCard .fa-note").count(), 0)

    def test_a_missing_figure_is_fetched_once(self):
        text = self.card(dict(self.FA, usage=None))
        self.assertIn("Not fetched yet", text)
        self.advance(25000, step=5000)
        self.assertEqual(self.site.count("/api/flightaware/status"), 1)

    def test_budget_used_up(self):
        text = self.card(dict(self.FA, count=900, paused=True, cost_estimate=4.5))
        self.assertIn("Paused until December 1", text)
        self.assertIn("budget is used up", text)

    def test_refused_key_is_named_in_the_banner(self):
        text = self.card(dict(self.FA, key_state="invalid",
                              last_lookup=dict(self.FA["last_lookup"], status="invalid_key")),
                         severity="warn", failing=["flightaware"],
                         health_reason="FlightAware didn't accept the API key — routes come from the free list")
        self.assertIn("Key not accepted", text)
        self.assertIn("key refused", text)
        self.assertIn("FlightAware key", self.page.inner_text(".overall"))


class TestStatusCollectorCard(BrowserCase):
    """v3.4.178: the Collector card names why writes stopped. Amber when Aerodrome
    is waiting (maintenance, no aircraft), red when something needs fixing."""

    BASE = {"ok": False, "warn": False, "last_write_seconds_ago": 290, "records_per_sec_5m": 0.6,
            "records_sample_count_5m": 190, "error": "No writes in 290s (threshold 45s)",
            "last_poll_error": None, "consecutive_failed_polls": 0, "threshold_seconds": 45,
            "activity": None, "empty_seconds": None, "receiver_hearing": None,
            "feed_static_seconds": None, "save_error": None, "last_poll_seconds_ago": 3}

    def card(self, **collector):
        st = _status_page({"enabled": False, "ok": True})
        st["components"]["collector"] = dict(self.BASE, **collector)
        self.site.api["/api/status"] = st
        self.open("/status")
        self.advance(1000)
        self.assertNotIn("Failed to load status", self.page.inner_text("body"))
        self.assertEqual(self.errors, [])
        light = self.page.get_attribute("#collectorCard .light", "class")
        return self.text("#collectorCard").replace("\xa0", " "), light

    def test_running(self):
        text, light = self.card(ok=True, state="running", last_write_seconds_ago=4)
        self.assertIn("Running", text)
        self.assertIn("4 s ago", text)
        self.assertIn("ok", light)
        self.assertEqual(self.page.locator("#collectorCard .coll-note").count(), 0)

    def test_maintenance_is_amber_and_names_the_job(self):
        text, light = self.card(state="paused", warn=True, activity={"kind": "stats", "seconds": 247})
        for want in ("Paused for maintenance", "4 min 50 s ago",
                     "Refreshing the database’s statistics", "Started 4 min 7 s ago",
                     "Collection resumes when it’s done", "every 6 hours"):
            self.assertIn(want, text)
        self.assertIn("warn", light)
        text, _ = self.card(state="paused", warn=True, activity={"kind": "clock", "seconds": 64},
                            last_write_seconds_ago=-3600)
        self.assertIn("Waiting for the system clock to be set", text)
        self.assertNotIn("resumes when it’s done", text)
        self.assertIn("Last write\n—", text)

    def test_no_aircraft(self):
        text, light = self.card(state="no_aircraft", warn=True, empty_seconds=372, receiver_hearing=False)
        for want in ("No aircraft reported", "reported no aircraft for 6 min 12 s",
                     "isn’t hearing anything", "receiver’s own map page", "dump1090 or readsb"):
            self.assertIn(want, text)
        self.assertIn("warn", light)
        text, _ = self.card(state="no_aircraft", warn=True, empty_seconds=372, receiver_hearing=None)
        self.assertNotIn("hearing", text)

    def test_stale_receiver(self):
        text, light = self.card(state="receiver_stale", feed_static_seconds=250)
        for want in ("Receiver data not updating", "hasn’t changed for 4 min 10 s",
                     "doesn’t save the same data twice", "Restart the decoder"):
            self.assertIn(want, text)
        self.assertIn("bad", light)

    def test_save_errors_say_what_to_do_and_escape_the_error(self):
        text, light = self.card(state="save_error", save_error={
            "kind": "disk_full", "count": 5, "seconds_ago": 3,
            "message": "sqlite3.OperationalError: database or disk is full <b>x</b>"})
        for want in ("Can’t save sightings", "The last 5 polls couldn’t be saved: the disk is full",
                     "Configuration → Retention", "database or disk is full <b>x</b>"):
            self.assertIn(want, text)
        self.assertIn("bad", light)
        self.assertEqual(self.page.locator("#collectorCard b").count(), 0)
        text, _ = self.card(state="save_error", save_error={"kind": "other", "count": 1, "seconds_ago": 3,
                                                           "message": "KeyError: 'hex'"})
        self.assertIn("The last poll couldn’t be saved.", text)

    def test_receiver_error_not_polling_and_stalled(self):
        text, _ = self.card(state="receiver_error", consecutive_failed_polls=7,
                            last_poll_error="ConnectionError: refused")
        self.assertIn("The last 7 polls couldn’t read the receiver:", text)
        self.assertIn("ConnectionError: refused", text)
        text, _ = self.card(state="not_polling", last_poll_seconds_ago=371)
        self.assertIn("hasn’t polled the receiver for 6 min 11 s", text)
        text, _ = self.card(state="stalled")
        self.assertIn("No sightings saved for 4 min 50 s. Aerodrome expects one at least every 45 s.", text)
        text, light = self.card(state="starting", warn=True, last_write_seconds_ago=None)
        self.assertIn("Collection starts in a moment.", text)
        self.assertIn("warn", light)


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


class TestRoutesOnlyWithALivePosition(BrowserCase):
    """The callsign→route data is often wrong, so the server checks a route
    against where the plane is and hides one that doesn't fit. A page asks only
    when it has a live position to check against: the aircraft page for a plane
    that isn't being received, and the board for a contact without a position,
    show no route and don't ask."""

    ROUTE = {"found": True, "airline": "Example Air", "current_leg": None,
             "airports": [{"icao": "KAAA", "name": "Alpha"}, {"icao": "KBBB", "name": "Bravo"}]}

    def setUp(self):
        super().setUp()
        self.site.api["/api/live"] = _aircraft(1)              # A00001 / DEMO1 has a position
        self.site.api["/api/status"] = _status()
        self.site.api["/api/all/drill"] = {"ok": True, "total_count": 0, "sightings": []}
        self.site.api["/api/callsign/DEMO1/route"] = self.ROUTE
        self.site.api["/api/callsign/DEMO42/route"] = self.ROUTE
        for icao, cs in (("A00001", "DEMO1"), ("A00042", "DEMO42")):
            self.site.api[f"/api/aircraft/{icao}"] = {
                "icao": icao, "first_seen_at": 1700000000, "last_seen_at": 1700000000,
                "sighting_count": 3, "recent_sightings": [], "callsigns": [cs],
                "last_callsign": cs}

    def route_line(self):
        return self.page.evaluate("""() => { const l = document.getElementById('routeLine');
            return l && l.style.display !== 'none' ? l.textContent : null; }""")

    def test_a_plane_being_received_shows_its_checked_route(self):
        self.open("/aircraft/A00001")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.site.count("/api/callsign/DEMO1/route"), 1)
        self.assertIn("KAAA", self.route_line() or "")

    def test_the_sightings_list_shows_looked_up_routes(self):
        # v3.4.171: rows of a flight FlightAware confirmed carry its route.
        self.site.api["/api/all/drill"] = {"ok": True, "total_count": 2, "sightings": [
            {"seen_at": 1700000000, "callsign": "DEMO1", "altitude": 36000, "speed": 450, "lat": 40, "lon": -90,
             "route": {"origin": "KBOS", "dest": "KDEN", "source": "flightaware"}},
            {"seen_at": 1699990000, "callsign": "DEMO9", "altitude": 34000, "speed": 440, "lat": 40, "lon": -90}]}
        self.open("/aircraft/A00001")
        self.page.wait_for_timeout(500)
        rows = self.page.evaluate("[...document.querySelectorAll('.row-card.sightings-grid')].map(r => r.textContent)")
        self.assertEqual(len(rows), 2)
        self.assertIn("KBOS → KDEN", rows[0])
        self.assertIn("FA", rows[0])
        self.assertNotIn("→", rows[1])
        self.assertIn("ROUTE", self.text(".row-grid-header.sightings-grid").upper())

    def test_a_plane_not_being_received_shows_no_route(self):
        self.open("/aircraft/A00042")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.site.count("/api/callsign/DEMO42/route"), 0)
        self.assertIsNone(self.route_line())

    def queries(self, path):
        return [q for p, q in self.site.calls if p == path]

    def test_the_aircraft_page_asks_flightaware_and_labels_its_route(self):
        self.site.api["/api/callsign/DEMO1/route"] = dict(self.ROUTE, source="flightaware",
                                                          departed_at=1700000000, arrives_at=1700009000)
        self.open("/aircraft/A00001")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.queries("/api/callsign/DEMO1/route")[0].get("fa"), ["1"])
        self.assertIn("FlightAware", self.route_line() or "")

    def test_the_map_panel_shows_a_flightaware_routes_label_and_times(self):
        self.site.api["/api/callsign/DEMO1/route"] = dict(self.ROUTE, source="flightaware",
                                                          departed_at=1700000000, arrives_at=1700009000)
        self.site.api["/api/ui-config"] = {}
        self.open("/")
        self.advance(1000)
        self.page.click("#bLive .row-card[data-icao='A00001']")
        self.advance(800, step=200)
        self.assertEqual(self.queries("/api/callsign/DEMO1/route")[-1].get("fa"), ["1"])
        times = self.text("#roTimes")
        self.assertRegex(times, r"^Departed .+ · arrives about .+$")
        self.assertIn("FlightAware", self.text("#roAirline"))
        title = self.page.get_attribute("#roAirline .ro-fa", "title")
        self.assertIn("Contains AeroAPI data © FlightAware LLC", title)

    def test_a_free_list_route_has_no_label_or_times(self):
        self.site.api["/api/ui-config"] = {}
        self.open("/")
        self.advance(1000)
        self.page.click("#bLive .row-card[data-icao='A00001']")
        self.advance(800, step=200)
        self.assertFalse(self.page.is_visible("#roTimes"))
        self.assertNotIn("FlightAware", self.text("#roAirline"))

    def test_the_boards_rows_only_use_routes_already_looked_up(self):
        self.open("/board?mode=board")
        self.advance(1000)
        self.page.evaluate("routeFor({callsign: 'DEMO1', lat: 40, lon: -90})")
        self.page.wait_for_timeout(200)
        asked = self.queries("/api/callsign/DEMO1/route")
        self.assertTrue(asked)
        self.assertTrue(all(q.get("fa") == ["cached"] for q in asked), asked)

    def test_opening_a_planes_details_on_the_board_asks_flightaware(self):
        self.site.api["/api/callsign/DEMO1/route"] = lambda req: dict(
            self.ROUTE, **({"source": "flightaware", "departed_at": 1700000000, "arrives_at": 1700009000}
                           if "fa=1" in req.url else {}))
        self.open("/board?mode=radar")
        self.advance(1000)
        self.page.evaluate("routeFor(LIVE.find(a => a.icao === 'A00001'))")
        self.page.wait_for_timeout(200)
        self.page.evaluate("openDetail('A00001')")
        self.advance(600, step=200)
        self.assertEqual(self.queries("/api/callsign/DEMO1/route")[-1].get("fa"), ["1"])
        detail = self.page.evaluate("document.querySelector('.dp-rt') && document.querySelector('.dp-rt').textContent")
        self.assertIn("FlightAware", detail or "")
        self.assertRegex(detail or "", r"Departed .+ · arrives about .+")

    def test_the_board_doesnt_ask_without_a_position(self):
        self.open("/board?mode=board")
        self.advance(1000)
        self.assertIsNone(self.page.evaluate("routeFor({callsign: 'DEMO42'})"))
        self.page.wait_for_timeout(100)
        self.assertEqual(self.site.count("/api/callsign/DEMO42/route"), 0)


class TestAircraftSightingsFitOnPhones(BrowserCase):
    """v3.4.173: the Sightings list's six columns need 769 px, so on a narrower
    screen each row takes two lines instead of the page scrolling sideways."""

    def setUp(self):
        super().setUp()
        from datetime import datetime
        now = int(datetime.fromisoformat(self.START).timestamp())
        self.site.api["/api/live"] = {"aircraft": [], "receiver": {"lat": 40, "lon": -90, "distance_unit": "mi"}}
        self.site.api["/api/status"] = _status()
        self.site.api["/api/aircraft/A00001"] = {
            "icao": "A00001", "first_seen_at": now - 3 * 86400, "last_seen_at": now - 90000,
            "sighting_count": 2, "recent_sightings": [], "callsigns": ["DEMO1"], "last_callsign": "DEMO1"}
        self.site.api["/api/all/drill"] = {"ok": True, "total_count": 2, "sightings": [
            {"seen_at": now - 90000, "callsign": "DEMO1", "altitude": 36000, "speed": 450, "lat": 40.5, "lon": -90.5,
             "route": {"origin": "KBOS", "dest": "KDEN", "source": "flightaware"}},
            {"seen_at": now - 3 * 86400, "callsign": "DEMO1", "altitude": 34000, "speed": 440, "lat": 40.5,
             "lon": -90.5}]}

    def layout(self, width):
        self.page.set_viewport_size({"width": width, "height": 900})
        self.open("/aircraft/A00001", settle=600)
        return self.page.evaluate("""() => {
            const d = document.documentElement, row = document.querySelector('.row-card.sightings-grid');
            const cells = [...row.children].map(c => c.getBoundingClientRect());
            return {scroll: d.scrollWidth, client: d.clientWidth, right: Math.max(...cells.map(r => r.right)),
                    lines: new Set(cells.map(r => Math.round(r.top))).size, text: row.textContent}; }""")

    def test_a_phone_shows_every_column_without_scrolling_sideways(self):
        for width in (360, 390, 768):
            got = self.layout(width)
            self.assertEqual(got["scroll"], got["client"], f"page scrolls sideways at {width} px")
            self.assertLessEqual(got["right"], got["client"], f"a cell is off screen at {width} px")
            self.assertEqual(got["lines"], 2, f"rows should take two lines at {width} px")
            for part in ("DEMO1", "KBOS → KDEN", "36,000", "450"):
                self.assertIn(part, got["text"])
        self.assertEqual(self.errors, [])

    def test_a_wide_screen_keeps_one_line_a_row(self):
        got = self.layout(1180)
        self.assertEqual(got["lines"], 1)
        self.assertEqual(got["scroll"], got["client"])

    def test_one_day_ago_is_singular(self):
        self.layout(1180)
        times = self.page.evaluate("[...document.querySelectorAll('.row-card.sightings-grid')].map(r => r.children[0].textContent)")
        self.assertEqual(times, ["1 day ago", "3 days ago"])


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


class TestPerformanceTableSizesAreOptIn(BrowserCase):
    """v3.4.171: measuring every table's size reads the whole database, so the
    Performance page no longer waits for it — it offers the measurement, and
    once one exists it says when that was instead."""

    MEASURED = {"measured_at": 1781539200, "took_ms": 34_000}      # 11:00 on the page's day

    def open_perf(self, report, path="/performance"):
        self.site.api["/api/perf/diagnostics"] = report
        self.open(path)
        self.advance(1000)

    def test_a_normal_run_offers_the_measurement_instead_of_waiting_for_it(self):
        self.open_perf(_perf())
        body = self.text("#content")
        self.assertIn("Table sizes aren't measured in a normal run", body)
        self.assertIn("2.6 GB here, about 30 seconds", body)
        self.assertEqual(self.page.inner_text("#sizesBtn"), "Measure table sizes")

    def test_the_button_measures_and_the_page_then_says_when(self):
        self.open_perf(_perf())
        self.site.api["/api/perf/diagnostics"] = _perf(table_sizes=self.MEASURED)
        self.page.click("#sizesBtn")
        self.advance(1000, step=250)
        self.assertEqual(self.site.calls[-1], ("/api/perf/diagnostics", {"sizes": ["true"]}))
        body = self.text("#content")
        self.assertIn("Sizes measured today at", body)
        self.assertIn("(took 34 s)", body)
        self.assertEqual(self.errors, [])

    def test_measuring_keeps_the_legacy_flag_the_url_asked_for(self):
        self.open_perf(_perf(), path="/performance?include_legacy=true")
        self.page.click("#sizesBtn")
        self.advance(1000, step=250)
        self.assertEqual(self.site.calls[-1][1], {"include_legacy": ["true"], "sizes": ["true"]})

    def test_a_measurement_from_another_day_shows_its_date(self):
        self.open_perf(_perf(table_sizes={"measured_at": 1781539200 - 86_400, "took_ms": 30_500}))
        body = self.text("#content")
        self.assertIn("Sizes measured Jun 14", body)
        self.assertIn("(took 31 s)", body)
        self.assertNotIn("today at", body)

    def test_the_copied_report_says_whether_sizes_were_measured(self):
        self.open_perf(_perf())
        self.assertIn("Table sizes: not measured", self.page.evaluate("buildReportText(_lastReport)"))
        self.site.api["/api/perf/diagnostics"] = _perf(table_sizes=self.MEASURED)
        self.page.click("#sizesBtn")
        self.advance(1000, step=250)
        text = self.page.evaluate("buildReportText(_lastReport)")
        self.assertIn("Table sizes measured 2026-06-15 16:00:00 UTC (took 34 s)", text)


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
