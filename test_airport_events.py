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
     "runways": [[40.0123, -90.0, 39.9877, -90.0, "18", "36"]]},
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
        self.assertEqual(idx, {"idx_airport_events_icao", "idx_airport_events_at"})
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
        self.assertEqual([c["code"] for c in cards["airports_30d"]], ["ZZN1", "ZZA1"])

    def test_the_stats_page_has_an_airports_section(self):
        with mock.patch.object(server.tzwindow, "today_bounds", return_value=(T0 - 100, T0 + 7000)), \
                mock.patch.object(server.time, "time", return_value=float(T0 + 7000)):
            j = self.endpoint("/api/stats")()
        ids = [g["id"] for g in j["groups"]]
        self.assertEqual(ids[:2], ["today", "airports"])
        self.assertEqual(j["cards"]["airport_moves_today"]["total"], 11)


if __name__ == "__main__":
    unittest.main()
