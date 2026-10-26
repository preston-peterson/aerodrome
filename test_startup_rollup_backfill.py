"""Startup ordering: rollup backfills vs schema migrations, and the gate.

The one-time rollup backfills (sightings_hourly v2.50.0, military_hourly +
watchlist_hourly v3.4.6) used to run in daemon threads started by init_db,
concurrently with main.py's schema migrations. A backfill holds the write
lock for its whole INSERT…SELECT, so on an old database the migration hit
"database is locked", startup exited, systemd restarted it, and the backfill
started over — a restart loop with no web UI. Migrations v1, v5 and v16 also
READ those rollups, so they must run after the backfills, not beside them.

These tests pin: init_db finishes the backfills before it returns (no
threads); main.py's order (init_db → gate → migrations) yields the derived
data an in-order upgrade would; a backfill that fails (disk full) blocks the
migrations — nothing is stamped on an incomplete rollup — and the next start
backfills and migrates correctly; and the migration transaction takes the
write lock up front (BEGIN IMMEDIATE) so a concurrent commit can't fail it.

SQLite end-to-end: a fresh database, a simulated prior-version
upgrade (pre-v2.50 and pre-v3.4.6 shapes) and the read path (the rollups and
derived columns the Stats/Search queries read). All data is invented (a
made-up hex block, TST callsigns). No network, no root.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import collector
import schema_migrations
from test_update_apply import repo_guard

# Imported here, never inside a test that patches collector._open_db_conn:
# main and server bind that name when they are first imported.
import main  # noqa: E402

setUpModule, tearDownModule = repo_guard()

HEX0 = 0xF10000
TMP = os.environ.get("TMPDIR") or None


def _tmpdb(test):
    d = tempfile.TemporaryDirectory(dir=TMP)
    test.addCleanup(d.cleanup)
    return os.path.join(d.name, "t.db")


def _reset_backfill_states():
    for st, lk in ((collector._hourly_backfill_state, collector._hourly_backfill_lock),
                   (collector._military_backfill_state, collector._military_backfill_lock),
                   (collector._watchlist_backfill_state, collector._watchlist_backfill_lock)):
        with lk:
            st.update({"phase": "unknown", "started_at": None, "finished_at": None,
                       "rows_processed": 0, "rows_total": None, "error": None})


def _raw_rows(n, n_icao, label=None, source_type_every=0):
    now = int(time.time())
    for i in range(n):
        atype = "adsb_icao" if source_type_every and i % source_type_every == 0 else "B738"
        r = (f"{HEX0 + i % n_icao:06x}", f"TST{i % 900:03d}", 300.0,
             40.0, -90.0, float((i * 37) % 9000),  # some 0 altitudes
             atype, "Example Type", now - 5 * 86400 + i * 30)
        yield r + ((label,) if label is not None else ())


def _build_pre_v250(path, n=3000, n_icao=40):
    """v2.49-shape DB: raw table + seen_aircraft, no rollups, no meta, v0."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE all_sightings (id INTEGER PRIMARY KEY AUTOINCREMENT,
        icao TEXT NOT NULL, callsign TEXT DEFAULT '', speed REAL, lat REAL, lon REAL,
        altitude REAL, aircraft_type TEXT DEFAULT '', type_desc TEXT DEFAULT '',
        seen_at INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE seen_aircraft (icao TEXT PRIMARY KEY,
        first_seen_at INTEGER NOT NULL, first_callsign TEXT DEFAULT '',
        first_aircraft_type TEXT DEFAULT '')""")
    c.executemany("INSERT INTO all_sightings (icao,callsign,speed,lat,lon,altitude,"
                  "aircraft_type,type_desc,seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  _raw_rows(n, n_icao))
    c.executemany("INSERT INTO seen_aircraft VALUES (?,?,?,?)",
                  ((f"{HEX0 + k:06x}", int(time.time()) - 6 * 86400, "TST001", "B738")
                   for k in range(n_icao)))
    c.commit()
    c.close()


def _build_pre_v346(path, n=3000, n_icao=40, from_schema=15):
    """Current init_db + migrations, then rolled back to look like an install
    that predates the v3.4.6 military/watchlist rollups: flags removed, raw
    rows present (10% carrying a feed message-source value as the type, as
    pre-v3.4.138 ingest stored), schema stamped back to `from_schema`."""
    collector.init_db(path)
    c = sqlite3.connect(path)
    assert schema_migrations.apply_schema_migrations(c, "test")["ok"]
    c.execute("DELETE FROM _aerodrome_meta WHERE key IN "
              "('military_hourly_backfilled', 'watchlist_hourly_backfilled')")
    c.execute("DELETE FROM schema_version WHERE version > ?", (from_schema,))
    c.executemany("INSERT INTO military_sightings (icao,callsign,speed,lat,lon,altitude,"
                  "aircraft_type,type_desc,seen_at,special_label) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  _raw_rows(n, n_icao, "example-label", source_type_every=10))
    c.executemany("INSERT INTO watchlist_sightings (icao,callsign,speed,lat,lon,altitude,"
                  "aircraft_type,type_desc,seen_at,watchlist_label) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  _raw_rows(n // 3, n_icao, "watch-a", source_type_every=10))
    c.commit()
    c.close()


def _gate_trips(path):
    """main.start's migration gate, on its own."""
    return bool(collector.rollup_backfill_incomplete(path)) and \
        main._schema_current_version(path) < schema_migrations.CURRENT_SCHEMA_VERSION


def _startup(path):
    """main.start()'s DB sequence: init_db, the gate, then migrations on a
    fresh connection with the 60 s busy timeout."""
    collector.init_db(path)
    if _gate_trips(path):
        return {"ok": False, "refused": True}
    c = sqlite3.connect(path, timeout=60.0)
    try:
        return schema_migrations.apply_schema_migrations(c, "test")
    finally:
        c.close()


def _assert_derived_data_correct(test, path):
    """The read path: what Stats/Search read after an in-order upgrade."""
    c = sqlite3.connect(path)
    try:
        # v1 step 5d: lifetime sighting_count = SUM over the hourly rollup.
        got = dict(c.execute("SELECT icao, sighting_count FROM seen_aircraft"))
        want = dict(c.execute("SELECT icao, COUNT(*) FROM all_sightings GROUP BY icao"))
        test.assertEqual(got, want)
        # v5: min_nonzero_altitude filled for every bucket that had one.
        missing = c.execute("""
            SELECT COUNT(*) FROM sightings_hourly sh
            WHERE sh.min_nonzero_altitude IS NULL AND EXISTS (
              SELECT 1 FROM all_sightings a WHERE a.icao = sh.icao
                AND (a.seen_at / 3600) * 3600 = sh.hour_bucket AND a.altitude > 0)
        """).fetchone()[0]
        test.assertEqual(missing, 0)
        test.assertEqual(schema_migrations.current_version(c),
                         schema_migrations.CURRENT_SCHEMA_VERSION)
        test.assertEqual(collector.rollup_backfill_incomplete(path), [])
    finally:
        c.close()


class _RecordingThread(threading.Thread):
    names = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        _RecordingThread.names.append(self.name)


class TestInitDbBackfillsSynchronously(unittest.TestCase):
    def setUp(self):
        _reset_backfill_states()
        self.db = _tmpdb(self)
        _build_pre_v346(self.db)
        _reset_backfill_states()
        _RecordingThread.names = []

    def test_no_backfill_threads_and_rollups_done_on_return(self):
        with mock.patch.object(collector.threading, "Thread", _RecordingThread):
            collector.init_db(self.db)
        self.assertEqual([n for n in _RecordingThread.names if "backfill" in n], [])
        self.assertEqual(collector.get_military_backfill_status()["phase"], "complete")
        self.assertEqual(collector.get_watchlist_backfill_status()["phase"], "complete")
        c = sqlite3.connect(self.db)
        meta = dict(c.execute("SELECT key, value FROM _aerodrome_meta"))
        self.assertEqual(meta.get("military_hourly_backfilled"), "1")
        self.assertEqual(meta.get("watchlist_hourly_backfilled"), "1")
        self.assertGreater(c.execute("SELECT COUNT(*) FROM military_hourly").fetchone()[0], 0)
        c.close()

    def test_v16_cleanup_sees_backfilled_rollups(self):
        res = _startup(self.db)
        self.assertTrue(res["ok"], res)
        c = sqlite3.connect(self.db)
        for t in ("military_hourly", "watchlist_hourly"):
            n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE aircraft_type = 'adsb_icao'").fetchone()[0]
            self.assertEqual(n, 0, f"{t} kept message-source types: backfill ran after v16")
        c.close()


class TestPreV250UpgradeOrder(unittest.TestCase):
    def setUp(self):
        _reset_backfill_states()
        self.db = _tmpdb(self)
        _build_pre_v250(self.db)

    def test_migrations_see_the_filled_hourly_rollup(self):
        res = _startup(self.db)
        self.assertTrue(res["ok"], res)
        _assert_derived_data_correct(self, self.db)


class TestFreshInstall(unittest.TestCase):
    """A brand-new database: flags set in milliseconds, nothing gated."""

    def test_fresh_db_starts_and_sets_every_flag(self):
        _reset_backfill_states()
        db = _tmpdb(self)
        res = _startup(db)
        self.assertTrue(res["ok"], res)
        self.assertEqual(collector.rollup_backfill_incomplete(db), [])
        for fn in (collector.get_hourly_backfill_status, collector.get_military_backfill_status,
                   collector.get_watchlist_backfill_status):
            self.assertEqual(fn()["phase"], "complete")


class _FailingConn:
    """A rollup's INSERT…SELECT (sightings_hourly unless told otherwise)
    fails as on a full disk."""

    def __init__(self, real, table="sightings_hourly"):
        self._c = real
        self._table = table

    def execute(self, sql, *a):
        if f"INSERT OR REPLACE INTO {self._table}" in sql:
            raise sqlite3.OperationalError("database or disk is full")
        return self._c.execute(sql, *a)

    def __getattr__(self, k):
        return getattr(self._c, k)


class _FailingCount:
    """One rollup check's COUNT(*) fails once with a transient I/O error —
    only when that backfill check runs it (init_db runs the same SQL
    earlier for its own purposes, which must not use the fault up)."""

    def __init__(self, real, fn_name):
        self._c = real
        self._fn = fn_name

    def execute(self, sql, *a):
        if (sql.lstrip().upper().startswith("SELECT COUNT(*)")
                and sys._getframe(1).f_code.co_name == self._fn):
            raise sqlite3.OperationalError("disk I/O error")
        return self._c.execute(sql, *a)

    def __getattr__(self, k):
        return getattr(self._c, k)


class _Reached(Exception):
    """main.start got past the gate and the migrations (set_db_path)."""


class TestBackfillFailureBlocksMigrations(unittest.TestCase):
    """A backfill that fails (disk full) must not let the migrations stamp
    the schema as upgraded on an incomplete rollup: v1 would compute every
    lifetime sighting_count from an empty rollup (0) and v5 leave
    min_nonzero_altitude NULL — permanently, since nothing reruns them."""

    def setUp(self):
        _reset_backfill_states()
        self.db = _tmpdb(self)
        _build_pre_v250(self.db)
        real = collector._open_db_conn
        p = mock.patch.object(collector, "_open_db_conn",
                              lambda path, *a, **kw: _FailingConn(real(path, *a, **kw)))
        p.start()
        self.fault = p

    def test_gate_refuses_then_next_start_is_correct(self):
        res = _startup(self.db)                       # boot 1: disk full
        self.fault.stop()
        self.assertTrue(res.get("refused"), res)
        self.assertEqual(collector.rollup_backfill_incomplete(self.db),
                         ["hourly_rollup_backfilled"])
        c = sqlite3.connect(self.db)
        self.assertEqual(schema_migrations.current_version(c), 0)   # nothing migrated
        c.close()
        _reset_backfill_states()
        res = _startup(self.db)                       # boot 2: space freed
        self.assertTrue(res["ok"], res)
        self.assertEqual([m["version"] for m in res["applied"]][-1],
                         schema_migrations.CURRENT_SCHEMA_VERSION)
        _assert_derived_data_correct(self, self.db)

    def test_real_start_exits_1_before_migrating(self):
        tmp = Path(self.db).parent
        with mock.patch.object(main, "PID_FILE", tmp / ".tracker.pid"), \
                mock.patch("schema_migrations.apply_schema_migrations") as mig, \
                self.assertLogs("adsb.main", "ERROR") as logs:
            with self.assertRaises(SystemExit) as cm:
                main.start({"data": {"db_file": self.db}})
        self.fault.stop()
        self.assertEqual(cm.exception.code, 1)
        mig.assert_not_called()
        text = "\n".join(logs.output)
        self.assertIn("rollup build didn't finish", text)
        self.assertIn("hourly_rollup_backfilled", text)
        self.assertIn("database or disk is full", text)
        self.assertIn("nothing was changed", text)

    def tearDown(self):
        try:
            self.fault.stop()
        except RuntimeError:
            pass


class TestBackfillCountErrorBlocksMigrations(unittest.TestCase):
    """A rollup check whose COUNT(*) fails (a transient I/O error) must not
    read as "no rows": that would mark the rollup built without building it,
    the gate would pass, and the migrations would stamp lifetime counts of 0
    for good. It is an error instead: nothing migrates, the next start
    builds the rollup and migrates correctly."""

    CASES = (
        ("_backfill_hourly_if_needed", "hourly_rollup_backfilled", "sightings_hourly",
         _build_pre_v250, collector.get_hourly_backfill_status),
        ("_backfill_military_if_needed", "military_hourly_backfilled", "military_hourly",
         _build_pre_v346, collector.get_military_backfill_status),
        ("_backfill_watchlist_if_needed", "watchlist_hourly_backfilled", "watchlist_hourly",
         _build_pre_v346, collector.get_watchlist_backfill_status),
    )

    def test_failed_count_is_an_error_and_the_next_start_is_correct(self):
        for fn, flag, table, build, status in self.CASES:
            with self.subTest(fn):
                _reset_backfill_states()
                db = _tmpdb(self)
                build(db)
                c = sqlite3.connect(db)
                schema0 = schema_migrations.current_version(c)
                c.close()
                _reset_backfill_states()
                real = collector._open_db_conn
                with mock.patch.object(
                        collector, "_open_db_conn",
                        lambda p, *a, _fn=fn, **kw: _FailingCount(real(p, *a, **kw), _fn)):
                    res = _startup(db)                       # boot 1: COUNT fails
                self.assertTrue(res.get("refused"), res)
                self.assertIn(flag, collector.rollup_backfill_incomplete(db))
                st = status()
                self.assertEqual(st["phase"], "error")
                self.assertIn("disk I/O error", st["error"] or "")
                c = sqlite3.connect(db)
                self.assertEqual(schema_migrations.current_version(c), schema0)
                c.close()
                _reset_backfill_states()
                res = _startup(db)                           # boot 2: it works
                self.assertTrue(res["ok"], res)
                _assert_derived_data_correct(self, db)
                c = sqlite3.connect(db)
                self.assertGreater(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                c.close()

    def test_real_start_exits_1_with_the_error(self):
        _reset_backfill_states()
        db = _tmpdb(self)
        _build_pre_v250(db)
        _reset_backfill_states()
        real = collector._open_db_conn
        tmp = Path(db).parent
        with mock.patch.object(collector, "_open_db_conn",
                               lambda p, *a, **kw: _FailingCount(
                                   real(p, *a, **kw), "_backfill_hourly_if_needed")), \
                mock.patch.object(main, "PID_FILE", tmp / ".tracker.pid"), \
                mock.patch("schema_migrations.apply_schema_migrations") as mig, \
                self.assertLogs("adsb.main", "ERROR") as logs:
            with self.assertRaises(SystemExit) as cm:
                main.start({"data": {"db_file": db}})
        self.assertEqual(cm.exception.code, 1)
        mig.assert_not_called()
        text = "\n".join(logs.output)
        self.assertIn("hourly_rollup_backfilled", text)
        self.assertIn("disk I/O error", text)


class TestStartGate(unittest.TestCase):

    def test_start_exits_when_gate_trips(self):
        tmp = Path(_tmpdb(self)).parent
        with mock.patch.object(main, "PID_FILE", tmp / ".tracker.pid"), \
                mock.patch.object(main, "init_db"), \
                mock.patch("collector.rollup_backfill_incomplete",
                           return_value=["military_hourly_backfilled"]), \
                mock.patch.object(main, "_schema_current_version", return_value=3), \
                mock.patch("schema_migrations.apply_schema_migrations") as mig, \
                self.assertLogs("adsb.main", "ERROR"):
            with self.assertRaises(SystemExit) as cm:
                main.start({"data": {"db_file": str(tmp / "x.db")}})
        self.assertEqual(cm.exception.code, 1)
        mig.assert_not_called()

    def test_current_schema_with_a_failed_backfill_keeps_running(self):
        # The real main.start on an up-to-date database whose military
        # rollup build fails (disk full): no migration is pending, so the
        # gate must let it start (the Live page shows the error) — exiting
        # here would restart-loop every up-to-date install, and under the
        # update guard roll a healthy release back.
        _reset_backfill_states()
        db = _tmpdb(self)
        _build_pre_v346(db, from_schema=schema_migrations.CURRENT_SCHEMA_VERSION)
        _reset_backfill_states()
        real = collector._open_db_conn
        tmp = Path(db).parent
        with mock.patch.object(collector, "_open_db_conn",
                               lambda p, *a, **kw: _FailingConn(real(p, *a, **kw),
                                                                "military_hourly")), \
                mock.patch.object(main, "PID_FILE", tmp / ".tracker.pid"), \
                mock.patch.object(main, "set_db_path", side_effect=_Reached), \
                self.assertNoLogs("adsb.main", "ERROR"):
            with self.assertRaises(_Reached):
                main.start({"data": {"db_file": db}})
        self.assertIn("military_hourly_backfilled", collector.rollup_backfill_incomplete(db))
        self.assertEqual(collector.get_military_backfill_status()["phase"], "error")

    def test_current_version_is_read_only(self):
        db = _tmpdb(self)
        c = sqlite3.connect(db)
        self.assertEqual(schema_migrations.current_version(c), 0)
        self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='schema_version'")
                          .fetchone())
        c.close()

    def test_incomplete_reports_all_flags_when_meta_unreadable(self):
        db = _tmpdb(self)
        sqlite3.connect(db).close()
        self.assertEqual(collector.rollup_backfill_incomplete(db),
                         list(collector.ROLLUP_BACKFILL_FLAGS))


class TestMigrationTxnTakesWriteLockUpFront(unittest.TestCase):
    """A deferred BEGIN that reads before it writes fails INSTANTLY with
    'database is locked' (SQLITE_BUSY_SNAPSHOT, busy handler skipped) when
    another connection commits in between. BEGIN IMMEDIATE makes the other
    writer wait instead."""

    def test_concurrent_commit_cannot_fail_the_migration(self):
        db = _tmpdb(self)
        c = sqlite3.connect(db, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t (x)")
        c.commit()
        other_done = threading.Event()

        def other_writer():
            o = sqlite3.connect(db, timeout=5.0)
            o.execute("INSERT INTO t VALUES ('other')")
            o.commit()
            o.close()
            other_done.set()

        def fake_migration(conn):
            conn.execute("SELECT COUNT(*) FROM t").fetchone()  # read first
            th = threading.Thread(target=other_writer, daemon=True)
            th.start()
            other_done.wait(0.5)  # deferred: other commits; immediate: it waits
            conn.execute("INSERT INTO t VALUES ('migration')")  # then write

        with mock.patch.object(schema_migrations, "MIGRATIONS",
                               [(9999, "test-only", fake_migration)]):
            res = schema_migrations.apply_schema_migrations(c, "test")
        self.assertTrue(res["ok"], res)
        self.assertTrue(other_done.wait(5.0))
        rows = sorted(r[0] for r in c.execute("SELECT x FROM t"))
        self.assertEqual(rows, ["migration", "other"])
        c.close()


if __name__ == "__main__":
    unittest.main()
