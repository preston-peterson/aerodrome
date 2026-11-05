"""Watchlist entries by tail number: local arithmetic for US tails, hexdb's v2
registration lookup for the rest, retries without a restart, and one shared map
for the collector, Search and Live. Runs the real fetch_and_store and endpoints
against a migrated scratch DB. hexdb and the receiver are mocked; tails, hexes
and labels are invented unless a test says otherwise.
"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests
import yaml

import collector
import hexdb_resolver
from schema_migrations import apply_schema_migrations

REPO = Path(__file__).resolve().parent
AIRCRAFT = {"lat": 40.0, "lon": -90.0, "alt_baro": 12000, "gs": 300, "t": "B738", "squawk": "1200"}


def _resp(code=200, payload=None, bad_json=False):
    r = mock.Mock(status_code=code, headers={"Content-Type": "application/json"})
    r.text = json.dumps(payload)
    r.json = mock.Mock(side_effect=ValueError("not JSON")) if bad_json else (lambda: payload)
    r.raise_for_status = lambda: None
    return r


def _v2(*rows):
    return _resp(200, {"count": len(rows), "results": list(rows)})


def _row(hex_code="4CA001", reg="G-EXMP"):
    return {"hex": hex_code, "registration": reg, "manufacturer": "Demo Aircraft",
            "type": "Demo 1", "icao_type_code": "B738", "registered_owner": "Example Air",
            "operator_flag_code": "EXA", "images": None,
            "links": {"self": f"/api/v2/aircraft/{hex_code}"}}


class TestNNumber(unittest.TestCase):

    def test_real_registrations(self):
        """Public hexdb.io answers, one per form of registration."""
        for tail, icao in {"N1": "A00001", "N99999": "ADF7C7", "N88": "AC1B7B", "N80": "AADF83",
                           "N100A": "A004B4", "N101NN": "A009A4", "N401UA": "A4B21B",
                           "N510JB": "A66239", "N800AN": "AAE1EA", "N12003": "A05626",
                           "N14228": "A0ACDD", "N37255": "A43E5E",
                           "N76502": "AA568E"}.items():
            self.assertEqual(collector._n_number_to_icao(tail), icao, tail)

    def test_ordering_at_the_start_of_the_block(self):
        for tail, icao in {"N1A": "A00002", "N1AA": "A00003", "N1AB": "A00004",
                           "N1B": "A0001B", "N10": "A0025A", "N2": "A18D50"}.items():
            self.assertEqual(collector._n_number_to_icao(tail), icao, tail)
        self.assertEqual(int(collector._n_number_to_icao("N1234A"), 16),
                         int(collector._n_number_to_icao("N1234"), 16) + 1)
        self.assertEqual(int(collector._n_number_to_icao("N12340"), 16),
                         int(collector._n_number_to_icao("N1234"), 16) + 25)

    def test_not_n_numbers(self):
        for tail in ("", "N", "N0", "N01", "N123456", "N12345A", "N1234AB", "N1I", "N1O",
                     "N1ABC", "N12A3", "NA", "G-EXMP", "n1", "N1 "):
            self.assertIsNone(collector._n_number_to_icao(tail), tail)


class _TailCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        self._reset_tails()
        self.addCleanup(self._reset_tails)
        # A worker started by another test module may be alive in this process:
        # keep it off the tail table, and let the tests drive the real function.
        self.run_due = collector.run_due_tail_lookup
        for p in (mock.patch.object(collector, "run_due_tail_lookup", return_value=False),
                  mock.patch.object(hexdb_resolver, "enqueue", return_value=False),
                  mock.patch.object(hexdb_resolver, "start_worker")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    @staticmethod
    def _reset_tails():
        collector._tail_state.clear()
        collector._tail_map = {}
        collector._ICAO_CACHE.clear()
        collector._TYPE_CODE_CACHE.clear()

    def tails(self, *tails):
        self.cfg["watchlist"] = [{"tail": t, "label": f"Label {t}"} for t in tails]

    def hexdb(self, *answers):
        """Mock hexdb: each lookup takes the next answer; an exception is raised."""
        return mock.patch.object(collector.requests, "get", side_effect=list(answers))

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class TestLookup(_TailCase):

    def test_found_uses_v2_and_is_cached(self):
        self.tails("g-exmp ")                 # normalized
        with self.hexdb(_v2(_row())) as get:
            lookup = collector.build_watchlist_lookup(self.cfg)
            get.assert_not_called()           # never on the poll thread
            self.assertEqual(lookup["icao_map"], {})
            self.assertEqual(collector.watchlist_tail_status("G-EXMP"), "pending")
            self.assertTrue(self.run_due())
        args, kwargs = get.call_args
        self.assertEqual(args, ("https://hexdb.io/api/v2/aircraft",))
        self.assertEqual(kwargs["params"], {"registration": "G-EXMP"})
        self.assertEqual(collector.watchlist_tail_status("G-EXMP"), "found")
        self.assertEqual(collector.watchlist_tail_map(), {"G-EXMP": ["4CA001"]})
        self.assertEqual(collector.build_watchlist_lookup(self.cfg)["icao_map"],
                         {"4CA001": "Label g-exmp "})
        # Everything a hex lookup would have stored, so nothing asks again.
        self.assertEqual(
            self.q("SELECT registration, last_outcome, registered_owner, manufacturer, type_code "
                   "FROM hexdb_cache WHERE icao = '4CA001'"),
            [("G-EXMP", "positive", "Example Air", "Demo Aircraft", "B738")])
        self.assertEqual(self.q("SELECT kind, icao FROM hexdb_events"),
                         [("miss_positive", "4CA001")])
        self.assertEqual(collector._ICAO_CACHE["4CA001"], "G-EXMP")

    def test_second_process_life_uses_the_cache(self):
        self.tails("G-EXMP")
        with self.hexdb(_v2(_row())):
            collector.sync_watchlist_tails(self.cfg)
            self.run_due()
        self._reset_tails()                   # a restart
        with self.hexdb() as get:
            lookup = collector.build_watchlist_lookup(self.cfg)
            self.assertFalse(self.run_due())
        get.assert_not_called()
        self.assertEqual(list(lookup["icao_map"]), ["4CA001"])

    def test_every_airframe_with_that_registration_is_watched(self):
        self.tails("G-EXMP")
        with self.hexdb(_v2(_row("4CA001"), _row("4CA002"), _row("4CA001"))):
            collector.sync_watchlist_tails(self.cfg)
            self.run_due()
        self.assertEqual(collector.watchlist_tail_map()["G-EXMP"], ["4CA001", "4CA002"])
        self.assertEqual(set(collector.build_watchlist_lookup(self.cfg)["icao_map"]),
                         {"4CA001", "4CA002"})

    def test_other_registrations_and_bad_hexes_are_ignored(self):
        self.tails("G-EXMP")
        with self.hexdb(_v2(_row("4CA001", "G-EXMQ"), _row("<script>", "G-EXMP"), "not a row")):
            collector.sync_watchlist_tails(self.cfg)
            self.run_due()
        self.assertEqual(collector.watchlist_tail_status("G-EXMP"), "unknown")
        self.assertEqual(collector.watchlist_tail_map(), {})

    def test_unknown_is_rechecked_after_hours_not_each_poll(self):
        self.tails("G-EXMP")
        with self.hexdb(_v2()) as get:
            collector.sync_watchlist_tails(self.cfg)
            self.assertTrue(self.run_due())
            self.assertFalse(self.run_due())
            collector.sync_watchlist_tails(self.cfg)
            self.assertFalse(self.run_due())
        self.assertEqual(get.call_count, 1)
        self.assertEqual(collector.watchlist_tail_status("G-EXMP"), "unknown")
        wait = collector._tail_state["G-EXMP"]["next_try"] - time.time()
        self.assertAlmostEqual(wait, collector.TAIL_RETRY_UNKNOWN_SEC, delta=30)
        self.assertEqual(self.q("SELECT kind, icao FROM hexdb_events"), [("miss_negative", None)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM hexdb_cache"), [(0,)])

    def test_no_usable_answer_is_an_error_not_unknown(self):
        answers = {"404 page": _resp(404, bad_json=True), "429": _resp(429, {}),
                   "500": _resp(500, {}), "timeout": requests.Timeout("slow"),
                   "offline": requests.ConnectionError("down"), "bad JSON": _resp(bad_json=True),
                   "no results": _resp(200, {"count": 0}), "odd results": _resp(200, {"results": "x"}),
                   "not an object": _resp(200, [1])}
        for name, answer in answers.items():
            with self.subTest(name):
                self._reset_tails()
                self.tails("G-EXMP")
                with self.hexdb(answer):
                    collector.sync_watchlist_tails(self.cfg)
                    self.assertTrue(self.run_due())
                self.assertEqual(collector.watchlist_tail_status("G-EXMP"), "error")
                wait = collector._tail_state["G-EXMP"]["next_try"] - time.time()
                self.assertAlmostEqual(wait, collector.TAIL_RETRY_ERROR_SEC, delta=30)

    def test_an_outage_is_not_waited_out_tail_by_tail(self):
        self.tails("G-EXMP", "D-EXMP", "F-EXMP")
        with self.hexdb(requests.Timeout("slow")) as get:
            collector.sync_watchlist_tails(self.cfg)
            self.assertTrue(self.run_due())
            self.assertFalse(self.run_due())
        self.assertEqual(get.call_count, 1)
        for tail in ("G-EXMP", "D-EXMP", "F-EXMP"):
            self.assertEqual(collector.watchlist_tail_status(tail), "error")

    def test_logs_on_change_not_every_retry(self):
        self.tails("G-EXMP")
        with self.hexdb(requests.Timeout("slow"), requests.Timeout("slow"), _v2(_row())):
            collector.sync_watchlist_tails(self.cfg)
            with self.assertLogs(collector.logger, "WARNING") as logs:
                self.run_due()
            self.assertIn("G-EXMP", logs.output[0])
            collector._tail_state["G-EXMP"]["next_try"] = 0.0
            with self.assertNoLogs(collector.logger, "WARNING"):
                self.run_due()
            collector._tail_state["G-EXMP"]["next_try"] = 0.0
            self.run_due()
        self.assertEqual(collector.watchlist_tail_status("G-EXMP"), "found")

    def test_tail_removed_while_being_asked(self):
        self.tails("G-EXMP")
        collector.sync_watchlist_tails(self.cfg)

        def answer(*args, **kwargs):
            self.cfg["watchlist"] = []
            collector.sync_watchlist_tails(self.cfg)
            return _v2(_row())
        with mock.patch.object(collector.requests, "get", side_effect=answer):
            self.run_due()
        self.assertEqual(collector.watchlist_tail_map(), {})
        self.assertEqual(collector._tail_state, {})

    def test_entry_with_an_icao_needs_no_lookup(self):
        self.cfg["watchlist"] = [{"tail": "G-EXMP", "icao": "4ca009", "label": "Both"}]
        with self.hexdb() as get:
            lookup = collector.build_watchlist_lookup(self.cfg)
            self.assertFalse(self.run_due())
        get.assert_not_called()
        self.assertEqual(lookup["icao_map"], {"4CA009": "Both"})

    def test_cache_that_learned_the_tail_settles_it_without_asking(self):
        self.tails("G-EXMP")
        with self.hexdb(_v2()) as get:
            collector.sync_watchlist_tails(self.cfg)
            self.run_due()
            conn = sqlite3.connect(self.db)
            collector._hexdb_cache_write(conn, "4CA005", "G-EXMP", "positive", int(time.time()))
            conn.commit()
            conn.close()
            collector._tail_state["G-EXMP"]["next_try"] = 0.0
            self.assertFalse(self.run_due())
        self.assertEqual(get.call_count, 1)
        self.assertEqual(collector.watchlist_tail_map(), {"G-EXMP": ["4CA005"]})

    def test_missing_cache_table_is_not_fatal(self):
        empty = self.db + ".empty"
        sqlite3.connect(empty).close()
        self.addCleanup(lambda: os.unlink(empty))
        self.assertEqual(collector._tail_local_icaos("N1", empty), ["A00001"])
        self.assertEqual(collector._tail_local_icaos("G-EXMP", empty), [])
        self.assertEqual(collector._tail_local_icaos("G-EXMP", None), [])


class TestUsTails(_TailCase):

    def test_resolved_with_no_network(self):
        self.tails("N1AA")
        with self.hexdb() as get:
            lookup = collector.build_watchlist_lookup(self.cfg)
            self.assertFalse(self.run_due())
        get.assert_not_called()
        self.assertEqual(lookup["icao_map"], {"A00003": "Label N1AA"})
        self.assertEqual(collector.watchlist_tail_status("N1AA"), "found")

    def test_older_hex_from_the_cache_is_watched_too(self):
        conn = sqlite3.connect(self.db)
        collector._hexdb_cache_write(conn, "ABC001", "n1aa", "positive", int(time.time()))
        collector._hexdb_cache_write(conn, "ABC002", "N1AA", "negative", int(time.time()))
        conn.commit()
        conn.close()
        self.tails("N1AA")
        collector.sync_watchlist_tails(self.cfg)
        self.assertEqual(collector.watchlist_tail_map()["N1AA"], ["A00003", "ABC001"])


class TestSightings(_TailCase):
    """The symptom in the report: aircraft overhead, nothing recorded."""

    def poll(self, aircraft, lookup):
        payload = {"now": 0, "aircraft": aircraft}
        with mock.patch.object(collector.requests, "get", return_value=_resp(200, payload)):
            collector.fetch_and_store(self.cfg, lookup)

    def sightings(self):
        return self.q("SELECT icao, watchlist_label FROM watchlist_sightings")

    def test_us_tail_records_a_sighting(self):
        self.tails("N1AA")
        self.poll([dict(AIRCRAFT, hex="a00003", flight="DEMO1 ")],
                  collector.build_watchlist_lookup(self.cfg))
        self.assertEqual(self.sightings(), [("A00003", "Label N1AA")])

    def test_failed_lookup_recovers_without_a_restart(self):
        self.tails("G-EXMP")
        overhead = [dict(AIRCRAFT, hex="4ca001", flight="DEMO1 ")]
        lookup = collector.build_watchlist_lookup(self.cfg)
        sig = collector.watchlist_signature(self.cfg)
        with self.hexdb(requests.ConnectionError("down")):
            self.run_due()
        self.poll(overhead, lookup)
        self.assertEqual(self.sightings(), [])
        self.assertEqual(collector.watchlist_signature(self.cfg), sig)    # nothing to rebuild

        collector._tail_state["G-EXMP"]["next_try"] = 0.0
        with self.hexdb(_v2(_row())):
            self.run_due()
        self.assertNotEqual(collector.watchlist_signature(self.cfg), sig)  # run_collector rebuilds
        self.poll(overhead, collector.build_watchlist_lookup(self.cfg))
        self.assertEqual(self.sightings(), [("4CA001", "Label G-EXMP")])


class TestRunCollector(_TailCase):

    def test_picks_up_a_resolved_tail_while_running(self):
        import main
        self.tails("G-EXMP")
        self.cfg["receiver"]["poll_interval"] = 0.01
        seen = []
        stop = threading.Event()
        with mock.patch.object(main, "fetch_and_store",
                               side_effect=lambda cfg, lookup: seen.append(dict(lookup["icao_map"]))), \
                mock.patch.object(main, "check_capacity_alerts"), \
                mock.patch.object(main, "refresh_query_planner_stats"):
            thread = threading.Thread(target=main.run_collector, args=(self.cfg, stop), daemon=True)
            thread.start()
            try:
                self._wait(lambda: seen)
                self.assertEqual(seen[-1], {})
                with self.hexdb(_v2(_row())):
                    self.assertTrue(self.run_due())
                self._wait(lambda: seen[-1] == {"4CA001": "Label G-EXMP"})
            finally:
                stop.set()
                thread.join(timeout=5)

    def _wait(self, condition, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("condition not met in time")


class TestWorkerTurn(unittest.TestCase):

    def _turn(self, fake, icao=None):
        with mock.patch.object(hexdb_resolver, "_WORKER_SLEEP_SEC", 0), \
                mock.patch.object(hexdb_resolver, "_IDLE_SLEEP_SEC", 0), \
                mock.patch.object(hexdb_resolver, "_next_icao", return_value=icao):
            hexdb_resolver._worker_turn(fake)

    def test_tail_goes_before_the_queue(self):
        fake = mock.Mock(_ICAO_CACHE={"4CA001": "G-EXMP"})
        fake.run_due_tail_lookup.return_value = True
        self._turn(fake, "4CA001")
        self.assertEqual([c[0] for c in fake.method_calls],
                         ["run_due_tail_lookup", "resolve_icao_to_tail"])

    def test_a_failing_tail_does_not_stop_the_queue(self):
        fake = mock.Mock(_ICAO_CACHE={"4CA001": "G-EXMP"})
        fake.run_due_tail_lookup.side_effect = RuntimeError("boom")
        self._turn(fake, "4CA001")
        fake.resolve_icao_to_tail.assert_called_once_with("4CA001")


class TestServerAgrees(_TailCase):
    """Search, Live and the Watchlist tab read the collector's map, through the
    real endpoints."""

    def setUp(self):
        super().setUp()
        self.config_path = self.db + ".yaml"
        self.addCleanup(lambda: Path(self.config_path).unlink(missing_ok=True))
        self.tails("N1AA", "G-EXMP")
        Path(self.config_path).write_text(yaml.safe_dump(self.cfg))
        import server
        self.server = server
        self.app = server.get_app(self.cfg, self.config_path)

    def endpoint(self, path, method="GET"):
        return next(r.endpoint for r in self.app.routes
                    if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))

    def entries(self):
        return {e.get("tail") or e.get("icao"): e
                for e in self.endpoint("/api/watchlist/entries")()["entries"]}

    def _see(self, *hex_codes):
        payload = {"now": 0, "aircraft": [dict(AIRCRAFT, hex=h, flight="DEMO1 ") for h in hex_codes]}
        with mock.patch.object(collector.requests, "get", return_value=_resp(200, payload)):
            collector.fetch_and_store(self.cfg, collector.build_watchlist_lookup(self.cfg))

    def _search_wl(self):
        out = self.endpoint("/api/search")(q="watchlist", limit=50, offset=0, order="", dir="",
                                           from_ts=0, to_ts=0)
        return {row["icao"] for row in out["rows"]}

    def _live_flags(self, *hex_codes):
        payload = {"now": 0, "aircraft": [dict(AIRCRAFT, hex=h, flight="DEMO1 ") for h in hex_codes]}
        with mock.patch.object(self.server.req, "get", return_value=_resp(200, payload)):
            out = self.endpoint("/api/live")()
        return {a["icao"]: a["is_watchlist"] for a in out["aircraft"]}

    def test_statuses_on_the_entries_endpoint(self):
        got = self.entries()
        self.assertEqual(got["N1AA"]["tail_status"], "found")
        self.assertEqual(got["N1AA"]["tail_icaos"], ["A00003"])
        self.assertEqual(got["G-EXMP"]["tail_status"], "pending")
        with self.hexdb(_v2()):
            self.run_due()
        self.assertEqual(self.entries()["G-EXMP"]["tail_status"], "unknown")

    def test_ordinary_entries_are_returned_untouched(self):
        self.cfg["watchlist"] = [{"icao": "A1B2C3", "label": "Hex"},
                                 {"callsign": "DEMO", "label": "Prefix"}]
        self.assertEqual(self.endpoint("/api/watchlist/entries")()["entries"],
                         self.cfg["watchlist"])

    def test_display_fields_never_reach_the_config(self):
        self.entries()
        self.assertNotIn("tail_status", self.cfg["watchlist"][0])
        add = self.endpoint("/api/watchlist/add", "POST")
        out = add(SimpleNamespace(identifier="N1AB", id_type="tail", label="Added", delete_history=False))
        self.assertEqual(out["status"], "added")
        saved = yaml.safe_load(Path(self.config_path).read_text())["watchlist"]
        self.assertEqual({k for e in saved for k in e}, {"tail", "label"})

    def test_everything_agrees_once_resolved(self):
        with self.hexdb(_v2(_row())):
            self.run_due()
        self._see("a00003", "4ca001", "a00009")
        self.assertEqual(self._search_wl(), {"A00003", "4CA001"})
        self.assertEqual(self._live_flags("a00003", "4ca001", "a00009"),
                         {"A00003": True, "4CA001": True, "A00009": False})
        tab = self.endpoint("/api/watchlist")()
        self.assertEqual({a["latest"]["icao"] for a in tab["aircraft"]}, {"A00003", "4CA001"})

    def test_a_tail_resolved_after_startup_reaches_search_and_live(self):
        """The server's map used to be built at startup and on add/remove only."""
        self._see("4ca001")
        self.assertEqual(self._search_wl(), set())
        self.assertFalse(self._live_flags("4ca001")["4CA001"])
        with self.hexdb(_v2(_row())):
            self.run_due()
        self.assertEqual(self._search_wl(), {"4CA001"})
        self.assertTrue(self._live_flags("4ca001")["4CA001"])

    def test_add_is_visible_at_once_and_remove_clears_it(self):
        add = self.endpoint("/api/watchlist/add", "POST")
        remove = self.endpoint("/api/watchlist/remove", "POST")
        add(SimpleNamespace(identifier="n1ab", id_type="tail", label="Added", delete_history=False))
        self.assertEqual(self.entries()["N1AB"]["tail_icaos"], ["A00004"])
        self.assertTrue(self._live_flags("a00004")["A00004"])
        remove(SimpleNamespace(identifier="N1AB", id_type="tail", label="", delete_history=False))
        self.assertNotIn("N1AB", collector.watchlist_tail_map())
        self.assertFalse(self._live_flags("a00004")["A00004"])


class TestAnnotateEntryKinds(unittest.TestCase):
    """_annotate_watchlist marks rows for the entry kinds beyond icao/tail:
    callsign prefix and model substring. Hexes, callsigns and labels invented."""

    def annotate(self, entries, row):
        import server
        with mock.patch.object(server, "CONFIG", {"watchlist": entries}):
            return server._annotate_watchlist(dict(row))

    def test_callsign_prefix_entry_marks_matching_callsigns(self):
        entry = [{"callsign": "TEST", "label": "Test flights"}]
        hit = self.annotate(entry, {"icao": "A00001", "callsign": "TEST123"})
        self.assertTrue(hit["is_watchlist"])
        self.assertEqual(hit["watchlist_label"], "Test flights")
        miss = self.annotate(entry, {"icao": "A00002", "callsign": "DEMO1"})
        self.assertFalse(miss["is_watchlist"])
        self.assertIsNone(miss["watchlist_label"])

    def test_model_substring_entry_marks_matching_types(self):
        entry = [{"model": "DASH", "label": "Dash fleet"}]
        hit = self.annotate(entry, {"icao": "A00003", "callsign": "DEMO2",
                                    "aircraft_type": "DASH8"})
        self.assertTrue(hit["is_watchlist"])
        self.assertEqual(hit["watchlist_label"], "Dash fleet")
        by_desc = self.annotate(entry, {"icao": "A00004", "aircraft_type_desc": "Dash 8 Q400"})
        self.assertTrue(by_desc["is_watchlist"])
        miss = self.annotate(entry, {"icao": "A00005", "aircraft_type": "B738",
                                     "aircraft_type_desc": "Boeing 737-800"})
        self.assertFalse(miss["is_watchlist"])


if __name__ == "__main__":
    unittest.main()
