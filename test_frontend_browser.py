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
PAGES = {"/": "index.html", "/board": "board.html", "/status": "status.html"}


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
        # This page's poll and the shared banner's, each every 30 s (it was 17).
        self.assertLessEqual(self.site.count("/api/status") - before, 5)

    def test_the_list_keeps_what_it_had_when_live_fails(self):
        self.site.api["/api/live"] = (500, "boom")
        self.advance(6000)
        self.assertEqual(self.count_shown(), "1")


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


if __name__ == "__main__":
    unittest.main()
