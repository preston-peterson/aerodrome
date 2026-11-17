"""Takeoffs and landings (v3.4.184): airport_events.py's detector on invented
tracks, storage and de-duplication, the live hook in a real collector poll,
the fill-in from stored sightings (and carrying on after a restart), schema v21
on a fresh install and an upgrade, retention, and the read paths (the aircraft
endpoint and the Stats cards). Airports, hexes and callsigns are invented.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import airport_events as AE
import collector
import hexdb_resolver
import server
from config_validator import validate_config
from schema_migrations import CURRENT_SCHEMA_VERSION, _migration_v21_airport_events, apply_schema_migrations

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

REPO = Path(__file__).resolve().parent
LAT, LON = 40.0, -90.0
# ZZA1: a north-south runway 18/36 through (40, -90), field at 600 ft.
# ZZP1: parallel runways 13L/31R and 13R/31L 0.15 NM apart, 30 NM east.
# ZZN1: no runway data, 30 NM west.
AIRPORTS = [
    {"code": "ZZA1", "cat": "major", "lat": LAT, "lon": LON, "elev": 600, "name": "Example Major",
     "iata": "ZZA", "city": "Exampleville", "runways": [[40.0123, -90.0, 39.9877, -90.0, "18", "36"]]},
    {"code": "ZZP1", "cat": "regional", "lat": LAT, "lon": -89.35, "elev": 600, "name": "Example Parallels",
     "runways": [[40.01, -89.36, 39.99, -89.34, "13L", "31R"],
                 [40.01 - 0.0025, -89.36 - 0.0025, 39.99 - 0.0025, -89.34 - 0.0025, "13R", "31L"]]},
    {"code": "ZZN1", "cat": "small", "lat": LAT, "lon": -90.65, "elev": 600, "name": "Example Field",
     "runways": []},
]
T0 = 1794000000


def north(nm, lat=LAT):
    return lat + nm / 60.0


def takeoff_track(t0=T0, step=60, lon=LON):
    """Lifts off just past the south end heading north, climbing 1,500 ft a minute."""
    return [(t0 + i * step, north(-0.6 + 2.5 * i), lon, 700 + 1500 * i) for i in range(8)]


def landing_track(t0=T0, lon=LON):
    """Comes down from the north to 1 NM short of runway 18, then it's gone."""
    legs = [(8, 3200), (6.5, 2700), (5, 2200), (3.5, 1700), (2, 1200), (1.5, 900)]
    return [(t0 + i * 60, north(0.74 + d), lon, a) for i, (d, a) in enumerate(legs)]


def circuit(t0=T0, lon=LON):
    """One touch and go: down to the middle of runway 18, up again, heading south."""
    down = [(6, 2600), (5, 2200), (4, 1800), (3, 1400), (1.5, 1000), (0, 700)]
    up = [(-1.5, 1300), (-3, 1900), (-4.5, 2500), (-6, 3000), (-7.5, 3300)]
    return [(t0 + i * 60, north(d), lon, a) for i, (d, a) in enumerate(down + up)]


def cruise(t0=T0):
    return [(t0 + i * 60, north(-10 + 5 * i), LON, 35000) for i in range(6)]


class _Base(unittest.TestCase):

    def setUp(self):
        AE.reset()
        self.addCleanup(AE.reset)
        self.index = AE.AirportIndex(AIRPORTS)
        p = mock.patch.object(AE.airports_data, "near", return_value=AIRPORTS)
        p.start()
        self.addCleanup(p.stop)

    def run_tracker(self, tracks, tracker=None):
        """tracks: {icao: [(ts, lat, lon, alt)]}; fed in time order, then everything closed."""
        tr = tracker if tracker is not None else AE.Tracker(self.index)
        pts = sorted((p[0], icao, p) for icao, ps in tracks.items() for p in ps)
        events = []
        for ts, icao, p in pts:
            events += tr.close_stale(ts)
            events += tr.feed(icao, ts, "EXA1", p[1], p[2], p[3])
        events += tr.close_stale(pts[-1][0] + AE.GAP + 1)
        return [(e["icao"], e["kind"], e["airport"], e["runway"], e["at"]) for e in events]


class TestDetector(_Base):

    def test_a_takeoff_is_found_with_its_runway_once_it_climbs(self):
        tr = AE.Tracker(self.index)
        track = takeoff_track()
        got = [tr.feed("A00001", *p[:1], "EXA1", *p[1:]) for p in track]
        self.assertEqual(got[0], [])
        self.assertEqual(got[1], [], "not yet: two points can't show a climb")
        self.assertEqual([(e["kind"], e["airport"], e["runway"], e["at"]) for e in got[2]],
                         [("takeoff", "ZZA1", "36", T0)], "found on the third point, dated the first")
        self.assertEqual(sum(got[3:], []), [], "found once")

    def test_a_landing_is_found_when_the_aircraft_has_gone(self):
        tr = AE.Tracker(self.index)
        for p in landing_track():
            self.assertEqual(tr.feed("A00002", p[0], "EXA2", *p[1:]), [])
        self.assertEqual(tr.close_stale(T0 + 5 * 60 + AE.GAP), [], "not gone long enough yet")
        ev = tr.close_stale(T0 + 5 * 60 + AE.GAP + 1)
        self.assertEqual([(e["kind"], e["airport"], e["runway"], e["at"], e["callsign"]) for e in ev],
                         [("landing", "ZZA1", "18", T0 + 300, "EXA2")])

    def test_a_surface_position_ends_the_landing_at_once_and_a_new_takeoff_counts(self):
        tr = AE.Tracker(self.index)
        events = []
        for p in landing_track():
            events += tr.feed("A00003", p[0], "EXA3", *p[1:])
        events += tr.feed("A00003", T0 + 420, "EXA3", LAT, LON, None)        # on the ground
        self.assertEqual([e["kind"] for e in events], ["landing"])
        for i in range(5):                                                   # taxi, still in view
            events += tr.feed("A00003", T0 + 480 + 60 * i, "EXA3", LAT, LON, None)
        for p in takeoff_track(T0 + 900):
            events += tr.feed("A00003", p[0], "EXA3", *p[1:])
        self.assertEqual([(e["kind"], e["runway"]) for e in events], [("landing", "18"), ("takeoff", "36")])

    def test_a_dip_to_the_runway_is_a_touch_and_go(self):
        got = self.run_tracker({"A00004": circuit()})
        self.assertEqual(got, [("A00004", "touch_and_go", "ZZA1", "18", T0 + 300)])

    def test_back_to_back_circuits_are_each_found(self):
        track = circuit() + circuit(T0 + 660)
        got = self.run_tracker({"A00005": track})
        self.assertEqual([(k, at) for _, k, _, _, at in got], [("touch_and_go", T0 + 300), ("touch_and_go", T0 + 960)])

    def test_cruising_overhead_is_nothing(self):
        self.assertEqual(self.run_tracker({"A00006": cruise()}), [])

    def test_an_aircraft_already_in_view_isnt_a_takeoff(self):
        tr = AE.Tracker(self.index, head_unknown={"A00007"})
        self.assertEqual(self.run_tracker({"A00007": takeoff_track()}, tr), [])
        # …but after a gap its next visit is new
        tr2 = AE.Tracker(self.index, head_unknown={"A00007"})
        got = self.run_tracker({"A00007": cruise() + takeoff_track(T0 + 3600)}, tr2)
        self.assertEqual([k for _, k, *_ in got], ["takeoff"])

    def test_faster_polls_are_thinned_and_find_the_same_takeoff(self):
        fine = []
        for p, q in zip(takeoff_track(), takeoff_track()[1:]):
            for k in range(6):
                f = k / 6
                fine.append((p[0] + 10 * k, p[1] + (q[1] - p[1]) * f, LON, p[3] + (q[3] - p[3]) * f))
        got = self.run_tracker({"A00008": fine})
        self.assertEqual([(k, a, r) for _, k, a, r, _ in got], [("takeoff", "ZZA1", "36")])

    def test_close_parallel_runways_give_only_the_number(self):
        lon_mid = -89.35 - 0.00125
        track = [(T0 + i * 60, 40.0 - 0.01 - 0.03 * i, lon_mid + 0.0381 * i, 700 + 1500 * i) for i in range(6)]
        got = self.run_tracker({"A00009": track})
        self.assertEqual([(k, a, r) for _, k, a, r, _ in got], [("takeoff", "ZZP1", "13")])

    def test_an_airport_without_runways_still_counts_close_in(self):
        track = [(t, lat, -90.65, alt) for t, lat, _, alt in takeoff_track()]
        got = self.run_tracker({"A0000A": track})
        self.assertEqual([(k, a, r) for _, k, a, r, _ in got], [("takeoff", "ZZN1", "")])

    def test_out_of_order_and_repeated_points_are_ignored(self):
        tr = AE.Tracker(self.index)
        track = takeoff_track()
        events = []
        for p in [track[0], track[0], track[1], track[0], track[2]]:
            events += tr.feed("A0000B", p[0], "EXA1", *p[1:])
        self.assertEqual([e["kind"] for e in events], ["takeoff"])


class _Db(_Base):

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = str(self.dir / "history.db")
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self.cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        self.cfg["data"]["db_file"] = self.db
        self.cfg["watchlist"] = []
        self.cfg["receiver"].update(latitude=LAT, longitude=LON)

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def store(self, rows):
        """rows: [(icao, ts, lat, lon, alt)] into all_sightings, in the given order."""
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO all_sightings (icao, callsign, lat, lon, altitude, seen_at) "
                         "VALUES (?, 'EXA1', ?, ?, ?, ?)", [(i, la, lo, a, t) for i, t, la, lo, a in rows])
        conn.commit()
        conn.close()

    def events(self):
        return self.q("SELECT icao, kind, airport, runway, at FROM airport_events ORDER BY at, icao")


class TestSchema(_Db):

    def test_a_fresh_install_has_the_table_and_its_indexes(self):
        cols = {r[1] for r in self.q("PRAGMA table_info(airport_events)")}
        self.assertEqual(cols, {"id", "icao", "callsign", "kind", "airport", "runway", "at"})
        idx = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='airport_events'")}
        self.assertEqual(idx, {"idx_airport_events_icao", "idx_airport_events_at", "idx_airport_events_airport"})
        self.assertEqual(self.q("SELECT MAX(version) FROM schema_version")[0][0], CURRENT_SCHEMA_VERSION)

    def test_an_upgrade_from_v20_adds_it(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE airport_events")
        conn.execute("DELETE FROM schema_version WHERE version >= 21")
        conn.commit()
        result = apply_schema_migrations(conn, "test")
        conn.close()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual((result["starting_version"], result["ending_version"]), (20, CURRENT_SCHEMA_VERSION))
        self.assertIn(("airport_events",), self.q("SELECT name FROM sqlite_master WHERE type='table'"))

    def test_a_rerun_keeps_its_rows(self):
        conn = sqlite3.connect(self.db)
        AE.record(conn, [{"icao": "A00001", "kind": "takeoff", "airport": "ZZA1", "runway": "36", "at": T0}])
        _migration_v21_airport_events(conn)
        conn.commit()
        conn.close()
        self.assertEqual(self.q("SELECT COUNT(*) FROM airport_events")[0][0], 1)


class TestRecord(_Db):

    def test_the_same_event_twice_is_kept_once_but_circuits_minutes_apart_are_kept(self):
        conn = sqlite3.connect(self.db)
        e = {"icao": "A00001", "kind": "takeoff", "airport": "ZZA1", "runway": "36", "at": T0, "callsign": "EXA1"}
        self.assertEqual(AE.record(conn, [e, dict(e, at=T0 + 120)]), 1)
        tg = dict(e, kind="touch_and_go")
        self.assertEqual(AE.record(conn, [tg, dict(tg, at=T0 + 360), dict(tg, at=T0 + 400)]), 2)
        self.assertEqual(AE.record(conn, [dict(e, airport="ZZN1")]), 1, "another airport is another event")
        conn.commit()
        conn.close()


class TestLiveInAPoll(_Db):
    """The real fetch_and_store, the receiver mocked: a takeoff is stored by the third poll."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(hexdb_resolver, "enqueue", return_value=False),
                  mock.patch.object(hexdb_resolver, "start_worker")):
            p.start()
            self.addCleanup(p.stop)

    def poll(self, ts, aircraft):
        payload = {"now": ts, "aircraft": aircraft}
        resp = mock.Mock(status_code=200, headers={"Content-Type": "application/json"})
        resp.text = json.dumps(payload)
        resp.json = lambda: payload
        resp.raise_for_status = lambda: None
        with mock.patch.object(collector.requests, "get", return_value=resp), \
                mock.patch.object(collector.time, "time", return_value=float(ts)):
            collector.fetch_and_store(self.cfg, collector.build_watchlist_lookup(self.cfg))

    def test_a_takeoff_is_stored_as_it_happens_and_sets_where_the_fill_in_stops(self):
        for t, lat, lon, alt in takeoff_track()[:4]:
            self.poll(t, [{"hex": "a00001", "flight": "EXA1 ", "lat": lat, "lon": lon, "alt_baro": alt,
                           "gs": 160, "t": "B738"}])
        self.assertEqual(self.events(), [("A00001", "takeoff", "ZZA1", "36", T0)])
        state = json.loads(self.q("SELECT value FROM _aerodrome_meta WHERE key = ?", AE.META_KEY)[0][0])
        self.assertEqual(state["horizon_ts"], T0)

    def test_a_detection_error_never_stops_the_poll(self):
        with mock.patch.object(AE.Tracker, "feed", side_effect=RuntimeError("boom")):
            self.poll(T0, [{"hex": "a00001", "flight": "EXA1", "lat": LAT, "lon": LON, "alt_baro": 700}])
        self.assertEqual(self.q("SELECT COUNT(*) FROM all_sightings")[0][0], 1)


class TestFillIn(_Db):

    def history(self):
        rows = []
        for icao, track in (("A00001", takeoff_track(T0)), ("A00002", landing_track(T0 + 3000)),
                            ("A00003", circuit(T0 + 6000)), ("A00004", cruise(T0 + 9000))):
            rows += [(icao, t, la, lo, a) for t, la, lo, a in track]
        rows.sort(key=lambda r: r[1])
        return rows

    def start_live(self, now):
        conn = sqlite3.connect(self.db)
        AE.on_poll(conn, self.cfg, [], now)
        conn.commit()
        conn.close()

    def test_it_finds_what_live_detection_would_have_and_stops_at_the_horizon(self):
        self.store(self.history())
        self.start_live(T0 + 20000)
        later = [("A00009", t, la, lo, a) for t, la, lo, a in takeoff_track(T0 + 20000)]
        self.store(later)                                   # live's, after the horizon
        self.assertEqual(AE.backfill_status(sqlite3.connect(self.db))["state"], "running")
        self.assertTrue(AE.backfill_step(self.db, self.cfg))
        self.assertEqual(self.events(), [("A00001", "takeoff", "ZZA1", "36", T0),
                                         ("A00002", "landing", "ZZA1", "18", T0 + 3300),
                                         ("A00003", "touch_and_go", "ZZA1", "18", T0 + 6300)])
        self.assertEqual(AE.backfill_status(sqlite3.connect(self.db)), {"state": "done", "pct": 100})
        self.assertTrue(AE.backfill_step(self.db, self.cfg), "nothing left to do")

    def test_it_carries_on_after_a_restart_without_doubling_up(self):
        self.store(self.history())
        self.start_live(T0 + 20000)
        with mock.patch.object(AE, "BACKFILL_BATCH", 7):
            self.assertFalse(AE.backfill_step(self.db, self.cfg, budget=0))   # one small slice
            mid = json.loads(self.q("SELECT value FROM _aerodrome_meta WHERE key = ?", AE.META_KEY)[0][0])
            self.assertEqual(mid["cursor"], 7)
            self.assertGreater(AE.backfill_status(sqlite3.connect(self.db))["pct"], 0)
            AE.reset()                                                     # a restart
            for _ in range(20):
                if AE.backfill_step(self.db, self.cfg, budget=0):
                    break
        self.assertEqual([k for _, k, *_ in self.events()], ["takeoff", "landing", "touch_and_go"])

    def test_it_waits_for_live_detection_to_set_the_horizon(self):
        self.store(self.history())
        self.assertFalse(AE.backfill_step(self.db, self.cfg))
        self.assertEqual(self.events(), [])

    def test_without_a_receiver_position_nothing_runs(self):
        self.cfg["receiver"]["latitude"] = None
        self.store(self.history())
        self.start_live(T0 + 20000)
        self.assertFalse(AE.backfill_step(self.db, self.cfg))
        self.assertEqual(self.events(), [])

    def test_an_empty_database_is_done_at_once(self):
        self.start_live(T0)
        self.assertTrue(AE.backfill_step(self.db, self.cfg))


class TestRetention(_Db):

    def test_events_older_than_airport_days_are_removed(self):
        now = int(collector.time.time())
        conn = sqlite3.connect(self.db)
        for at in (now - 400 * 86400, now - 300 * 86400, now - 10):
            AE.record(conn, [{"icao": "A00001", "kind": "landing", "airport": "ZZA1", "at": at}])
        conn.commit()
        conn.close()
        ret = dict(self.cfg["retention"])
        self.assertEqual(ret["airport_days"], 365)
        collector.cleanup_old_data(self.db, ret)
        self.assertEqual(self.q("SELECT COUNT(*) FROM airport_events")[0][0], 2)
        ret.pop("airport_days")                            # an older config: a year
        collector.cleanup_old_data(self.db, ret)
        self.assertEqual(self.q("SELECT COUNT(*) FROM airport_events")[0][0], 2)
        collector.cleanup_old_data(self.db, dict(ret, airport_days=30))
        self.assertEqual(self.q("SELECT COUNT(*) FROM airport_events")[0][0], 1)

    def test_the_setting_is_validated(self):
        def errs(v):
            cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
            cfg["retention"]["airport_days"] = v
            return [e for e in validate_config(cfg) if e[0] == "retention.airport_days"]
        self.assertEqual(errs(365), [])
        self.assertEqual(errs(3650), [], "longer than all_days is fine")
        self.assertTrue(errs(0))
        self.assertTrue(errs("a year"))
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        del cfg["retention"]["airport_days"]
        self.assertEqual([e for e in validate_config(cfg) if e[0].startswith("retention.airport")], [])


class TestReadPaths(_Db):

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        ev = [("takeoff", "ZZA1", "36", T0, "EXA1"), ("landing", "ZZN1", "", T0 + 1800, "EXA1"),
              ("touch_and_go", "ZZN1", "", T0 + 3000, "EXA2"), ("touch_and_go", "ZZN1", "", T0 + 3360, "EXA2"),
              ("touch_and_go", "ZZN1", "", T0 + 3720, "EXA2"), ("takeoff", "ZZN1", "", T0 + 4000, "EXA2"),
              ("landing", "ZZA1", "18", T0 + 6000, "EXA2")]
        AE.record(conn, [{"icao": "A00001", "kind": k, "airport": a, "runway": r, "at": at, "callsign": cs}
                         for k, a, r, at, cs in ev])
        AE.record(conn, [{"icao": "A00002", "kind": "landing", "airport": "ZZA1", "runway": "18", "at": T0 + 100}])
        conn.commit()
        conn.close()
        cfg_path = self.dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))
        self.app = server.get_app(self.cfg, str(cfg_path))

    def endpoint(self, path):
        return next(r.endpoint for r in self.app.routes if getattr(r, "path", "") == path)

    def test_an_aircrafts_history_folds_circuits_and_names_its_latest_flight(self):
        j = self.endpoint("/api/aircraft/{icao}/airports")(icao="a00001", limit=8)
        self.assertEqual([(e["kind"], e["airport"], e["count"], e["at"], e["until"]) for e in j["events"]],
                         [("landing", "ZZA1", 1, T0 + 6000, T0 + 6000), ("takeoff", "ZZN1", 1, T0 + 4000, T0 + 4000),
                          ("touch_and_go", "ZZN1", 3, T0 + 3000, T0 + 3720),
                          ("landing", "ZZN1", 1, T0 + 1800, T0 + 1800), ("takeoff", "ZZA1", 1, T0, T0)])
        self.assertEqual(j["total"], 5)
        self.assertEqual([(e["kind"], e["airport"]) for e in j["recent"]], [("takeoff", "ZZN1"), ("landing", "ZZA1")])
        self.assertEqual(j["events"][0]["name"], "Example Major")
        self.assertEqual({a["code"]: (a["landings"], a["takeoffs"], a["touch_and_go"]) for a in j["airports"]},
                         {"ZZN1": (1, 1, 3), "ZZA1": (1, 1, 0)})
        self.assertEqual(j["since"], T0)
        self.assertEqual(len(self.endpoint("/api/aircraft/{icao}/airports")(icao="A00001", limit=2)["events"]), 2)

    def test_an_unknown_aircraft_is_empty_and_a_bad_hex_is_refused(self):
        j = self.endpoint("/api/aircraft/{icao}/airports")(icao="A0FFFF", limit=8)
        self.assertEqual((j["events"], j["recent"], j["total"]), ([], [], 0))
        with self.assertRaises(server.HTTPException):
            self.endpoint("/api/aircraft/{icao}/airports")(icao="nothex", limit=8)

    def test_stats_cards_count_today_and_the_busiest_airports(self):
        tz = None
        cards = AE.stats_cards(sqlite3.connect(self.db), self.cfg, T0 - 100, T0 + 7000, tz,
                               ["airport_moves_today", "airports_today", "airports_30d"])
        # 2 takeoffs + 3 landings + 3 touch-and-goes (each a landing and a takeoff)
        self.assertEqual(cards["airport_moves_today"]["total"], 11)
        self.assertEqual(cards["airport_moves_today"]["airports"], 2)
        rows = {r["code"]: (r["landed"], r["took_off"]) for r in cards["airports_today"]["rows"]}
        self.assertEqual(rows, {"ZZN1": (4, 4), "ZZA1": (2, 1)})
        self.assertEqual(sum(sum(r["hours"]) for r in cards["airports_today"]["rows"]), 11)
        self.assertEqual([c["code"] for c in cards["airports_30d"]["rows"]], ["ZZN1", "ZZA1"])
        self.assertEqual(cards["airports_30d"]["fill_in"]["state"], "waiting")

    def test_the_stats_page_has_an_airports_section(self):
        with mock.patch.object(server.tzwindow, "today_bounds", return_value=(T0 - 100, T0 + 7000)), \
                mock.patch.object(server.time, "time", return_value=float(T0 + 7000)):
            j = self.endpoint("/api/stats")()
        ids = [g["id"] for g in j["groups"]]
        self.assertEqual(ids[:2], ["today", "airports"])
        self.assertEqual(j["cards"]["airport_moves_today"]["total"], 11)



class TestAirportSummary(_Db):
    """v3.4.185: the Live map's airport panel (GET /api/airports/{code}/summary)."""

    NOW = T0 + 50000
    DAY = T0 + 40000

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        ev = [("A00001", "landing", "ZZA1", "18", self.NOW - 600, "EXA1"),
              ("A00002", "takeoff", "ZZA1", "36", self.NOW - 1200, "EXA2"),
              ("A00003", "touch_and_go", "ZZA1", "18", self.NOW - 1500, "DEMO3"),
              ("A00004", "touch_and_go", "ZZA1", "18", self.NOW - 1700, "DEMO4"),     # another aircraft, between
              ("A00003", "touch_and_go", "ZZA1", "18", self.NOW - 1900, "DEMO3"),
              ("A00005", "landing", "ZZA1", "36", self.NOW - 7200, "EXA5"),           # earlier today
              ("A00006", "landing", "ZZA1", "18", self.DAY - 3600, "EXA6"),           # yesterday
              ("A00001", "landing", "ZZN1", "", self.NOW - 300, "EXA1")]              # another airport
        AE.record(conn, [{"icao": i, "kind": k, "airport": a, "runway": r, "at": at, "callsign": cs}
                         for i, k, a, r, at, cs in ev])
        conn.executemany("INSERT INTO seen_aircraft (icao, first_seen_at, aircraft_type, category) VALUES (?, 1, ?, ?)",
                         [("A00001", "B738", "commercial"), ("A00002", "B738", "commercial"),
                          ("A00003", "C172", "general_aviation"), ("A00004", "C172", "general_aviation"),
                          ("A00005", "C130", "military")])
        conn.execute("INSERT INTO watchlist_hourly (icao, hour_bucket, watchlist_label, sighting_count, first_seen_at, "
                     "last_seen_at) VALUES ('A00002', ?, 'Example', 1, ?, ?)",
                     (self.NOW - self.NOW % 3600, self.NOW - 60, self.NOW - 60))
        conn.commit()
        conn.close()

    def summary(self, code="ZZA1"):
        conn = sqlite3.connect(self.db)
        try:
            return AE.airport_summary(conn, self.cfg, code, self.NOW, self.DAY)
        finally:
            conn.close()

    def test_todays_counts_runways_in_use_and_the_airport(self):
        j = self.summary()
        self.assertEqual((j["code"], j["name"], j["cat"], j["elev"], j["runways"]), ("ZZA1", "Example Major", "major", 600, 1))
        self.assertEqual((j["distance"], j["direction"]), (0, "N"))
        # landings 2 + touch-and-goes 3; takeoffs 1 + touch-and-goes 3; last hour: the landing, takeoff, 3 T&Gs
        self.assertEqual(j["today"], {"landed": 5, "took_off": 4, "touch_and_go": 3, "last_hour": 8})
        self.assertEqual(j["in_use"], {"landing": ["18"], "taking_off": ["18", "36"], "flow": "south"})

    def test_latest_folds_each_aircrafts_circuits_but_not_two_aircraft_together(self):
        latest = self.summary()["latest"]
        self.assertEqual([(m["icao"], m["kind"], m["count"]) for m in latest],
                         [("A00001", "landing", 1), ("A00002", "takeoff", 1), ("A00003", "touch_and_go", 1),
                          ("A00004", "touch_and_go", 1), ("A00003", "touch_and_go", 1), ("A00005", "landing", 1)])
        self.assertEqual(latest[0]["type"], "B738")

    def test_todays_visitors(self):
        v = self.summary()["visitors"]
        self.assertEqual(v["aircraft"], 5)
        self.assertEqual(v["types"], [{"type": "B738", "n": 2}, {"type": "C172", "n": 2}, {"type": "C130", "n": 1}])
        self.assertEqual(v["mix"], {"commercial": 2, "general_aviation": 2, "military": 1})
        self.assertEqual((v["military"], v["watchlist"]), (1, 1))

    def test_a_code_not_near_the_receiver_is_none_and_the_endpoint_says_404(self):
        self.assertIsNone(self.summary("ZZZZ"))
        cfg_path = self.dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))
        app = server.get_app(self.cfg, str(cfg_path))
        ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/airports/{code}/summary")
        with self.assertRaises(server.HTTPException) as cm:
            ep(code="ZZZZ")
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(server.HTTPException) as cm:
            ep(code="no/pe")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(ep(code="zza1")["code"], "ZZA1")



class TestAirportPage(_Db):
    """v3.4.186: the airport page (GET /api/airports/{code}, /moves, /airport/{code}) and Search's airport match."""

    NOW = T0 + 50000
    D = 86400

    def setUp(self):
        super().setUp()
        import designators
        self.al_code, self.al_name = sorted(designators.AIRLINES.items())[0]
        self.utc = __import__("datetime").timezone.utc
        self.day = self.NOW - self.NOW % self.D                       # today's midnight, UTC
        N, D, al = self.NOW, self.D, self.al_code
        ev = [("A00001", "landing", "ZZA1", "18", N - 40 * D, "EXA1"),         # history starts here, 40 days ago
              ("A00002", "takeoff", "ZZA1", "36", N - 5 * D, al + "12"),       # an airline
              ("A00003", "touch_and_go", "ZZA1", "18", N - 2 * D, "DEMO3"),    # one circuit session…
              ("A00003", "touch_and_go", "ZZA1", "18", N - 2 * D + 400, "DEMO3"),
              ("A00005", "landing", "ZZA1", "36", N - 3 * D, "MIL21"),         # military
              ("A00001", "landing", "ZZA1", "18", N - 1200, "EXA1"),           # today, been here before
              ("A00006", "landing", "ZZA1", "18", N - 600, "EXA6"),            # today, first time here
              ("A00001", "landing", "ZZN1", "", N - 300, "EXA1")]              # another airport
        conn = sqlite3.connect(self.db)
        AE.record(conn, [{"icao": i, "kind": k, "airport": a, "runway": r, "at": at, "callsign": cs}
                         for i, k, a, r, at, cs in ev])
        conn.executemany("INSERT INTO seen_aircraft (icao, first_seen_at, aircraft_type, category, registration) "
                         "VALUES (?, 1, ?, ?, ?)",
                         [("A00001", "B738", "commercial", ""), ("A00002", "B738", "commercial", ""),
                          ("A00003", "C172", "general_aviation", "N0DEMO"), ("A00005", "C130", "military", ""),
                          ("A00006", "E75L", "commercial", "")])
        conn.execute("INSERT INTO watchlist_hourly (icao, hour_bucket, watchlist_label, sighting_count, first_seen_at, "
                     "last_seen_at) VALUES ('A00005', ?, 'Example', 1, ?, ?)",
                     (self.NOW - 3 * D - (self.NOW - 3 * D) % 3600, self.NOW - 3 * D, self.NOW - 3 * D))
        conn.commit()
        conn.close()

    def page(self, code="ZZA1", tz=None):
        conn = sqlite3.connect(self.db)
        try:
            return AE.airport_page(conn, self.cfg, code, self.NOW, self.day, tz or self.utc)
        finally:
            conn.close()

    def test_the_header_adds_iata_city_runway_names_and_since(self):
        j = self.page()
        self.assertEqual((j["code"], j["iata"], j["city"], j["runway_names"]), ("ZZA1", "ZZA", "Exampleville", ["18/36"]))
        self.assertEqual((j["since"], j["history_start"]), (self.NOW - 40 * self.D, self.NOW - 40 * self.D))
        self.assertEqual(j["today"], {"landed": 2, "took_off": 0, "touch_and_go": 0, "last_hour": 2})
        self.assertIsNone(self.page("ZZZZ"))

    def test_runways_radio_and_links_from_airport_info(self):
        info = Path(tempfile.mkdtemp()) / "airport-info.json"
        self.addCleanup(shutil.rmtree, info.parent)
        info.write_text(json.dumps({"source": "test", "built": "2026-01-01", "count": 2, "airports": [
            ["ZZA1", "ZZA", 1, "https://example.com/", "", [["18/36", 9000, 150, "Concrete", 1]], [["Tower", "118.300"]]],
            ["ZZQQ", "", 0, "", "", [["9/27", 3000, 60, "Asphalt", 0]], []]]}), encoding="utf-8")
        AE.airports_data._info_cache.update(key=None, rows=None)
        self.addCleanup(AE.airports_data._info_cache.update, key=None, rows=None)
        with mock.patch.object(AE.airports_data, "INFO", info):
            self.assertEqual(self.page()["info"], {
                "ident": "ZZA", "faa_plates": True, "website": "https://example.com/", "wikipedia": "",
                "runways": [["18/36", 9000, 150, "Concrete", 1]], "radio": [["Tower", "118.300"]]})
            self.assertIsNone(self.page("ZZN1")["info"], "nothing known about it")

    def test_a_day_chart_from_the_first_day_of_history(self):
        s = self.page()["series"]
        from datetime import datetime
        self.assertEqual(s["start"], datetime.fromtimestamp(self.NOW - 40 * self.D, self.utc).date().isoformat())
        self.assertEqual(len(s["landed"]), 41)
        self.assertEqual((s["landed"][0], s["took_off"][0]), (1, 0))
        self.assertEqual((s["landed"][-1], s["took_off"][-1]), (2, 0))                 # today
        self.assertEqual((s["landed"][-3], s["took_off"][-3]), (2, 2))                 # a touch and go is both
        self.assertEqual((s["landed"][-6], s["took_off"][-6]), (0, 1))
        self.assertEqual(sum(s["landed"]) + sum(s["took_off"]), 1 + 1 + 4 + 1 + 2)

    def test_local_days_follow_the_clock_change(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Chicago")
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM airport_events")
        ats = [self.NOW - h * 3600 - 1800 for h in range(0, 24 * 45, 7)]   # every 7 hours across the DST change
        AE.record(conn, [{"icao": "A0%04X" % i, "kind": "landing", "airport": "ZZA1", "runway": "18", "at": at,
                          "callsign": ""} for i, at in enumerate(ats)])
        conn.commit()
        conn.close()
        day = int(datetime.fromtimestamp(self.NOW, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        conn = sqlite3.connect(self.db)
        try:
            j = AE.airport_page(conn, self.cfg, "ZZA1", self.NOW, day, tz)
        finally:
            conn.close()
        start = datetime.fromisoformat(j["series"]["start"]).date()
        want = [0] * len(j["series"]["landed"])
        hours = [0] * 24
        for at in ats:
            lt = datetime.fromtimestamp(at, tz)
            want[(lt.date() - start).days] += 1
            if lt.date() >= datetime.fromtimestamp(self.NOW, tz).date() - timedelta(days=29):
                hours[lt.hour] += 1
        self.assertEqual(j["series"]["landed"], want)
        self.assertEqual(j["hours"], hours)

    def test_runway_use_and_flows_over_30_days(self):
        u = self.page()["runway_use"]
        self.assertEqual(u["landing"], [["18", 80], ["36", 20]])
        self.assertEqual(u["taking_off"], [["18", 67], ["36", 33]])
        self.assertEqual([(f["flow"], f["share"]) for f in u["flows"]], [("south", 75), ("north", 25)])
        self.assertEqual(u["flows"][0]["landing"], ["18"])
        self.assertEqual(u["with_runway"], 100)

    def test_visitors_over_30_days(self):
        m = self.page()["month"]
        self.assertEqual(m["types"][0], {"type": "C172", "name": m["types"][0]["name"], "n": 4})
        self.assertEqual([t["type"] for t in m["types"]], ["C172", "B738", "C130", "E75L"])
        self.assertEqual(m["operators"], [{"name": self.al_name, "n": 1}])
        self.assertEqual((m["aircraft"], m["first_seen"], m["watchlist"]), (5, 4, 1))
        self.assertEqual(m["mix"], {"commercial": 3, "general_aviation": 1, "military": 1})
        self.assertEqual([(a["icao"], a["n"]) for a in m["frequent"]],
                         [("A00003", 4), ("A00001", 1), ("A00002", 1), ("A00005", 1), ("A00006", 1)])
        self.assertEqual(m["frequent"][0]["who"], "N0DEMO")                     # no airline: the tail number
        self.assertEqual(m["frequent"][2]["who"], self.al_name)
        self.assertEqual(m["military"], [{"icao": "A00005", "type": "C130", "callsign": "MIL21", "n": 1}])

    def test_new_here_today_only_once_there_is_history_before_today(self):
        nt = self.page()["new_today"]
        self.assertEqual(nt, {"n": 1, "rows": [{"icao": "A00006", "type": "E75L", "callsign": "EXA6",
                                                 "at": self.NOW - 600}]})
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM airport_events WHERE at < ?", (self.day,))
        conn.commit()
        conn.close()
        j = self.page()
        self.assertEqual(j["new_today"], {"n": 0, "rows": []})
        self.assertIsNone(j["month"]["first_seen"])        # nothing older than 30 days to compare with

    def test_latest_moves_fold_sessions_and_page_back(self):
        moves = self.page()["moves"]
        self.assertEqual([(m["icao"], m["kind"], m["count"]) for m in moves["items"]],
                         [("A00006", "landing", 1), ("A00001", "landing", 1), ("A00003", "touch_and_go", 2),
                          ("A00005", "landing", 1), ("A00002", "takeoff", 1), ("A00001", "landing", 1)])
        self.assertIsNone(moves["next"])
        self.assertEqual((moves["items"][4]["type"], moves["items"][4]["operator"]), ("B738", self.al_name))
        conn = sqlite3.connect(self.db)
        try:
            first = AE.airport_moves(conn, self.cfg, "ZZA1", limit=2)
            second = AE.airport_moves(conn, self.cfg, "ZZA1", before=first["next"], limit=2)
        finally:
            conn.close()
        self.assertEqual([m["icao"] for m in first["items"]], ["A00006", "A00001"])
        self.assertEqual(first["next"], self.NOW - 1200)
        self.assertEqual([m["icao"] for m in second["items"]], ["A00003"])        # its two circuits, folded

    def test_an_airport_with_nothing_yet(self):
        j = self.page("ZZP1")
        self.assertIsNone(j["since"])
        self.assertEqual(j["moves"], {"items": [], "next": None})
        self.assertEqual(j["month"]["aircraft"], 0)
        self.assertEqual(sum(j["series"]["landed"]), 0)

    def test_search_finds_an_airport_by_its_code_or_iata(self):
        self.assertEqual([a["code"] for a in AE.search_airports(self.cfg, "zza")], ["ZZA1"])
        self.assertEqual(AE.search_airports(self.cfg, "B738 ZZA1,")[0],
                         {"code": "ZZA1", "iata": "ZZA", "name": "Example Major", "city": "Exampleville", "cat": "major"})
        self.assertEqual(AE.search_airports(self.cfg, "ZZ Canada"), [])
        self.assertEqual(AE.search_airports({}, "ZZA1"), [])                      # no receiver position

    def test_the_endpoints_and_the_page(self):
        cfg_path = self.dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))
        app = server.get_app(self.cfg, str(cfg_path))
        ep = lambda path: next(r.endpoint for r in app.routes if getattr(r, "path", "") == path)
        for path in ("/api/airports/{code}", "/api/airports/{code}/moves"):
            with self.assertRaises(server.HTTPException) as cm:
                ep(path)(code="ZZZZ")
            self.assertEqual(cm.exception.status_code, 404)
            with self.assertRaises(server.HTTPException) as cm:
                ep(path)(code="no/pe")
            self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(ep("/api/airports/{code}")(code="zza1")["iata"], "ZZA")
        moves = ep("/api/airports/{code}/moves")(code="ZZA1", before=self.NOW - 1000, limit=2)
        self.assertTrue(moves["ok"])
        self.assertEqual([m["icao"] for m in moves["items"]], ["A00001", "A00003"])
        html = ep("/airport/{code}")(code="ZZA1").body.decode()
        self.assertIn("<title>Airport — Aerodrome</title>", html)
        found = ep("/api/search")(q="ZZA", limit=5, offset=0, order="", dir="", from_ts=0, to_ts=0, preset="")
        self.assertEqual([a["code"] for a in found["airports"]], ["ZZA1"])
        later = ep("/api/search")(q="ZZA", limit=5, offset=5, order="", dir="", from_ts=0, to_ts=0, preset="")
        self.assertEqual(later["airports"], [])


if __name__ == "__main__":
    unittest.main()
