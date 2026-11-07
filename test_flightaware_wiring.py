"""FlightAware AeroAPI wiring (v3.4.168): the saved key never goes back to the
browser, a save keeps it, the route endpoint asks FlightAware only when the page
says you opened the aircraft (fa=1), the two FlightAware endpoints, and a
watchlist alert's route line that costs a lookup only when the alert really goes
out. The FlightAware client itself is faked here (test_flightaware.py covers
it); callsigns, keys and airports are invented.
"""
import asyncio
import os
import shutil
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
import flightaware
import notifier
import route_resolver
import server
from schema_migrations import apply_schema_migrations

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

REPO = Path(__file__).resolve().parent
KEY = "EXAMPLEkey0000000000000000007c2e"
FOUND = {"status": "found", "route": {
    "origin": {"icao": "KBOS", "name": "Boston Logan Intl", "city": "Boston"},
    "destination": {"icao": "KDEN", "name": "Denver Intl", "city": "Denver"},
    "departed_at": 1794000000, "arrives_at": 1794015000,
    "fa_flight_id": "EXA412-1794000000-schedule-0001", "registration": "N412EX"}}


class FakeClient:
    def __init__(self, answer=None):
        self.answer = answer or {"status": "none"}
        self.cached_route = None
        self.calls = []

    def lookup(self, cfg, callsign):
        self.calls.append(("lookup", callsign))
        return self.answer

    def status(self, cfg):
        self.calls.append(("status",))
        return {"configured": bool((cfg or {}).get("api_key")), "key_last4": "7c2e", "key_state": "ok",
                "tested_at": 1, "month": "2026-11", "count": 212, "limit": 900, "paused": False,
                "resets_on": "2026-12-01", "cost_estimate": 1.06, "limit_cost": 4.5}

    def usage(self, cfg, force=False):
        self.calls.append(("usage",))
        return {"total_cost": 0.95, "total_discount_cost": 0.0, "total_calls": 190, "as_of": 1}

    def test_key(self, cfg):
        self.calls.append(("test_key",))
        return {"state": "ok", "message": "Key works."}

    def cached(self, callsign):
        self.calls.append(("cached", callsign))
        return self.cached_route

    def cached_usage(self):
        self.calls.append(("cached_usage",))
        return {"total_cost": 0.95, "total_discount_cost": 0.0, "total_calls": 190, "as_of": 1}


class _App(unittest.TestCase):
    FA = {"api_key": KEY, "monthly_limit": 900, "watchlist_lookups": False}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = self.db
        cfg["watchlist"] = []
        cfg["flightaware"] = dict(self.FA)
        self.cfg_path = self.dir / "config.yaml"
        self.cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.app = server.get_app(cfg, str(self.cfg_path))
        self.client = FakeClient()
        p = mock.patch.object(flightaware, "shared", side_effect=lambda path, **kw: self.client)
        p.start()
        self.addCleanup(p.stop)

    def endpoint(self, path, method="GET"):
        return next(r.endpoint for r in self.app.routes
                    if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


class TestTheKeyStaysOnTheServer(_App):

    def test_the_config_read_never_contains_the_key(self):
        out = self.endpoint("/api/config")()
        self.assertEqual(out["flightaware"]["api_key"], server.FA_KEY_KEPT)
        self.assertNotIn(KEY, repr(out))
        self.assertEqual(server.CONFIG["flightaware"]["api_key"], KEY, "the live config keeps the key")

    def test_no_key_reads_as_blank(self):
        server.CONFIG["flightaware"]["api_key"] = ""
        self.assertEqual(self.endpoint("/api/config")()["flightaware"]["api_key"], "")

    def save(self, fa):
        cfg = self.endpoint("/api/config")()
        cfg = yaml.safe_load(yaml.safe_dump(cfg))          # what the browser would send back
        cfg["flightaware"].update(fa)
        return self.endpoint("/api/config", "PUT")(types.SimpleNamespace(config=cfg))

    def saved_key(self):
        return (yaml.safe_load(self.cfg_path.read_text()).get("flightaware") or {}).get("api_key")

    def test_saving_with_the_placeholder_keeps_the_key(self):
        out = self.save({"monthly_limit": 500})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.saved_key(), KEY)
        self.assertEqual(server.CONFIG["flightaware"]["monthly_limit"], 500)
        self.assertNotIn("flightaware.api_key", out["changed"])

    def test_a_new_key_replaces_it(self):
        self.save({"api_key": "EXAMPLEkey1111111111111111111111"})
        self.assertEqual(self.saved_key(), "EXAMPLEkey1111111111111111111111")

    def test_a_blank_key_removes_it(self):
        self.save({"api_key": ""})
        self.assertEqual(self.saved_key(), "")

    def test_a_key_with_odd_characters_is_refused(self):
        out = self.save({"api_key": "not a key!"})
        self.assertEqual(out.status_code, 400)
        self.assertEqual(self.saved_key(), KEY)


class TestRouteAsksFlightAwareOnlyWhenOpened(_App):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(route_resolver, "resolve_route", side_effect=self.free_list)
        p.start()
        self.addCleanup(p.stop)
        self.free_asked = 0

    def free_list(self, conn, cs):
        self.free_asked += 1
        return {"ok": True, "found": True, "cached": True, "callsign": cs,
                "origin_icao": "KORD", "origin_name": "Chicago", "dest_icao": "KLAS", "dest_name": "Las Vegas",
                "airline": "Example Air",
                "airports": [{"icao": "KORD", "name": "Chicago", "lat": 41.9786, "lon": -87.9048},
                             {"icao": "KLAS", "name": "Las Vegas", "lat": 36.080101, "lon": -115.152}]}

    def ask(self, **kw):
        return asyncio.run(self.endpoint("/api/callsign/{callsign}/route")(callsign="EXA412", **kw))

    def test_an_opened_aircraft_gets_flightawares_route(self):
        self.client.answer = FOUND
        out = self.ask(lat=40.0, lon=-100.0, track=270.0, fa="1")
        self.assertEqual(out["source"], "flightaware")
        self.assertEqual((out["origin_icao"], out["origin_name"], out["dest_icao"], out["dest_name"]),
                         ("KBOS", "Boston", "KDEN", "Denver"))
        self.assertEqual((out["departed_at"], out["arrives_at"]), (1794000000, 1794015000))
        self.assertIsNone(out["current_leg"])
        self.assertEqual(self.free_asked, 0, "FlightAware answered, so the free list isn't asked")

    def test_cached_uses_a_route_already_looked_up_and_never_spends(self):
        # The display board's rows and cards (v3.4.170).
        self.client.cached_route = FOUND["route"]
        out = self.ask(lat=40.0, lon=-100.0, track=270.0, fa="cached")
        self.assertEqual((out["source"], out["origin_icao"], out["dest_icao"]), ("flightaware", "KBOS", "KDEN"))
        self.assertEqual(self.client.calls, [("cached", "EXA412")])
        self.assertEqual(self.free_asked, 0)

    def test_cached_with_nothing_cached_uses_the_free_list(self):
        out = self.ask(lat=40.5, lon=-95.0, track=255.0, fa="cached")
        self.assertNotEqual(out.get("source"), "flightaware")
        self.assertNotIn(("lookup", "EXA412"), self.client.calls)
        self.assertEqual(self.free_asked, 1)

    def test_without_fa_flightaware_is_never_asked(self):
        self.client.answer = FOUND
        out = self.ask(lat=40.0, lon=-87.9, track=270.0)
        self.assertEqual(self.client.calls, [])
        self.assertNotEqual(out.get("source"), "flightaware")

    def test_no_answer_from_flightaware_falls_back_to_the_free_list(self):
        for status in ("none", "paused", "busy", "error", "invalid_key"):
            self.client.answer = {"status": status}
            out = self.ask(lat=40.5, lon=-95.0, track=255.0, fa="1")
            self.assertNotEqual(out.get("source"), "flightaware", status)
        self.assertEqual(self.free_asked, 5)

    def test_no_key_never_asks(self):
        server.CONFIG["flightaware"]["api_key"] = ""
        self.ask(lat=40.5, lon=-95.0, track=255.0, fa="1")
        self.assertEqual(self.client.calls, [])

    def test_a_route_with_an_unknown_end_falls_back(self):
        self.client.answer = {"status": "found", "route": dict(FOUND["route"], destination=None)}
        out = self.ask(lat=40.5, lon=-95.0, track=255.0, fa="1")
        self.assertNotEqual(out.get("source"), "flightaware")


class TestFlightAwareEndpoints(_App):

    def test_status_has_the_numbers_and_never_the_key(self):
        out = self.endpoint("/api/flightaware/status")()
        self.assertEqual((out["count"], out["limit"], out["key_last4"]), (212, 900, "7c2e"))
        self.assertEqual(out["usage"]["total_cost"], 0.95)
        self.assertEqual(out["usage_url"], flightaware.USAGE_URL)
        self.assertNotIn(KEY, repr(out))

    def test_status_without_a_key_skips_flightawares_figure(self):
        server.CONFIG["flightaware"]["api_key"] = ""
        out = self.endpoint("/api/flightaware/status")()
        self.assertIsNone(out["usage"])
        self.assertNotIn(("usage",), self.client.calls)

    def test_test_key(self):
        out = self.endpoint("/api/flightaware/test", "POST")()
        self.assertTrue(out["ok"])
        self.assertIn(("test_key",), self.client.calls)

    def test_test_key_without_a_key(self):
        server.CONFIG["flightaware"]["api_key"] = ""
        out = self.endpoint("/api/flightaware/test", "POST")()
        self.assertEqual((out["ok"], out["state"]), (False, "no_key"))
        self.assertEqual(self.client.calls, [])


class TestStatusComponent(_App):
    """v3.4.169: /api/status carries a flightaware component when a key is set,
    built without calling FlightAware (every page polls /api/status)."""

    def status(self):
        with mock.patch.object(server.req, "get", side_effect=OSError("no receiver in tests")):
            return self.endpoint("/api/status")()

    def test_a_key_adds_the_component_without_calling_flightaware(self):
        fa = self.status()["components"]["flightaware"]
        self.assertEqual((fa["count"], fa["limit"], fa["key_last4"]), (212, 900, "7c2e"))
        self.assertEqual(fa["usage"]["total_cost"], 0.95)
        self.assertEqual(fa["usage_url"], flightaware.USAGE_URL)
        self.assertNotIn(("usage",), self.client.calls)
        self.assertNotIn(KEY, repr(fa))

    def test_no_key_no_component(self):
        server.CONFIG["flightaware"]["api_key"] = ""
        self.assertIsNone(self.status()["components"]["flightaware"])
        self.assertEqual(self.client.calls, [])


class TestWatchlistAlertRoute(unittest.TestCase):
    """The route line costs a lookup, so it's computed only for an alert that
    actually sends; the cooldown keeps it to one per alert."""

    def setUp(self):
        self.n = notifier.Notifier({"enabled": True, "url": "http://ntfy.example/topic",
                                    "rate_limit_per_hour": 0, "events": {"watchlist_hit": True},
                                    "cooldown_minutes": {"watchlist_hit": 10}})
        p = mock.patch.object(self.n, "_post_ntfy", return_value=(True, 200, None))
        self.posted = p.start()
        self.addCleanup(p.stop)
        self.asked = 0

    def line(self):
        self.asked += 1
        return "Route: KBOS Boston → KDEN Denver (FlightAware)"

    def test_the_line_is_added_to_a_sent_alert(self):
        self.assertTrue(self.n.notify("watchlist_hit", "Watchlist: demo", "body", aircraft_icao="A00001",
                                      extra_line=self.line))
        self.assertEqual(self.posted.call_args[0][2], "body\nRoute: KBOS Boston → KDEN Denver (FlightAware)")

    def test_an_alert_in_cooldown_never_asks(self):
        self.n.notify("watchlist_hit", "t", "b", aircraft_icao="A00001", extra_line=self.line)
        self.n.notify("watchlist_hit", "t", "b", aircraft_icao="A00001", extra_line=self.line)
        self.assertEqual(self.asked, 1)

    def test_a_failing_line_still_sends_the_alert(self):
        def boom():
            raise RuntimeError("no route")
        self.assertTrue(self.n.notify("watchlist_hit", "t", "b", aircraft_icao="A00002", extra_line=boom))
        self.assertEqual(self.posted.call_args[0][2], "b")

    def test_collector_line_only_when_switched_on(self):
        cfg = {"data": {"db_file": "/var/tmp/example/history.db"},
               "flightaware": {"api_key": KEY, "watchlist_lookups": False}}
        self.assertIsNone(collector._watchlist_route_line(cfg, "EXA412"))
        cfg["flightaware"]["watchlist_lookups"] = True
        cfg["flightaware"]["api_key"] = ""
        self.assertIsNone(collector._watchlist_route_line(cfg, "EXA412"))
        cfg["flightaware"]["api_key"] = KEY
        self.assertIsNone(collector._watchlist_route_line(cfg, "  "))
        client = FakeClient(FOUND)
        with mock.patch.object(flightaware, "shared", return_value=client):
            line = collector._watchlist_route_line(cfg, "exa412")
            self.assertEqual(client.calls, [], "nothing is looked up until the alert sends")
            self.assertEqual(line(), "Route: KBOS Boston → KDEN Denver (FlightAware)")
            self.assertEqual(client.calls, [("lookup", "EXA412")])
            client.answer = {"status": "paused"}
            self.assertIsNone(line())


if __name__ == "__main__":
    unittest.main()
