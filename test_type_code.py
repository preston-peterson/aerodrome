"""Aircraft type code (v3.4.138): the feed's `t` vs the message-source
`type`, and hexdb's ICAOTypeCode as the fallback for feeds without `t`
(stock readsb installs, dump1090-fa/PiAware).

All hexdb HTTP is mocked — no network. Fixture hexes/registrations are
invented.
"""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import collector
import hexdb_resolver
from schema_migrations import apply_schema_migrations


def _reset_caches():
    collector._ICAO_CACHE.clear()
    collector._TYPE_CODE_CACHE.clear()


def _hexdb_response(payload, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


class TestFeedTypeCode(unittest.TestCase):

    def tearDown(self):
        _reset_caches()

    def test_t_is_used_and_upper_cased(self):
        self.assertEqual(collector.feed_type_code({"t": " a320 "}), "A320")

    def test_message_source_type_is_rejected(self):
        # readsb / dump1090-fa put the message source in `type`.
        for src in ("adsb_icao", "adsb_icao_nt", "adsr_icao", "tisb_icao",
                    "tisb_trackfile", "adsc", "mlat", "mode_s", "other",
                    "unknown"):
            with self.subTest(src=src):
                self.assertEqual(collector.feed_type_code({"type": src}), "")

    def test_upper_case_designator_in_type_still_accepted(self):
        self.assertEqual(collector.feed_type_code({"type": "B738"}), "B738")

    def test_junk_and_non_strings_rejected(self):
        for raw in ({"t": "<img src=x>"}, {"t": "A3200"}, {"t": 737},
                    {"t": None}, {"t": ""}, {}):
            with self.subTest(raw=raw):
                self.assertEqual(collector.feed_type_code(raw), "")

    def test_hexdb_code_fills_in_when_feed_has_none(self):
        collector._TYPE_CODE_CACHE["A1B2C3"] = "A321"
        self.assertEqual(collector.aircraft_type_for(
            {"hex": "a1b2c3", "type": "adsb_icao"}), "A321")
        # The feed's own code wins over hexdb's.
        self.assertEqual(collector.aircraft_type_for(
            {"hex": "a1b2c3", "t": "A20N"}), "A20N")

    def test_normalize_stock_readsb_record(self):
        ac = collector.normalize({"hex": "a1b2c4", "type": "mlat",
                                  "flight": "DEMO1 "})
        self.assertEqual(ac["aircraft_type"], "")


class _DbCase(unittest.TestCase):
    """A migrated scratch DB wired in as the resolver's cache store."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        collector.init_db(self.db)
        conn = sqlite3.connect(self.db)
        apply_schema_migrations(conn, "test")
        conn.close()
        self._prev_db = collector._db_path
        collector.set_db_path(self.db)
        _reset_caches()

    def tearDown(self):
        collector.set_db_path(self._prev_db)
        _reset_caches()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except FileNotFoundError:
                pass

    def _row(self, icao):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT registration, last_outcome, type_code FROM hexdb_cache "
                "WHERE icao = ?", (icao,)).fetchone()
        finally:
            conn.close()


class TestHexdbTypeCodeCache(_DbCase):

    def test_check_returns_type_code_and_treats_null_as_stale(self):
        now = int(time.time())
        conn = sqlite3.connect(self.db)
        try:
            collector._hexdb_cache_write(conn, "A1B2C3", "N100EX", "positive", now,
                                         registered_owner="Example Air",
                                         manufacturer="Airbus", type_code="A321")
            collector._hexdb_cache_write(conn, "A1B2C4", "N200EX", "positive", now,
                                         registered_owner="Example Air")
            collector._hexdb_cache_write(conn, "A1B2C5", None, "negative", now)
            # A pre-v15 row: written before type_code existed.
            conn.execute(
                "INSERT INTO hexdb_cache (icao, registration, resolved_at, "
                "last_outcome, hit_count, registered_owner, type_code) "
                "VALUES ('A1B2C6','N300EX',?, 'positive',0,'Example Air',NULL)",
                (now,))
            self.assertEqual(collector._hexdb_cache_check(conn, "A1B2C3", now),
                             ("fresh_positive", "N100EX", "A321"))
            # hexdb had no type: '' is a fresh answer, not a re-fetch.
            self.assertEqual(collector._hexdb_cache_check(conn, "A1B2C4", now),
                             ("fresh_positive", "N200EX", ""))
            self.assertEqual(collector._hexdb_cache_check(conn, "A1B2C5", now),
                             ("fresh_negative", None, ""))
            self.assertEqual(collector._hexdb_cache_check(conn, "A1B2C6", now),
                             ("stale", None, None))
        finally:
            conn.close()

    def test_resolve_captures_type_code(self):
        resp = _hexdb_response({"Registration": "N100EX", "ICAOTypeCode": "A321",
                                "RegisteredOwners": "Example Air",
                                "Manufacturer": "Airbus"})
        with mock.patch.object(collector.requests, "get", return_value=resp):
            self.assertEqual(collector.resolve_icao_to_tail("a1b2c3"), "N100EX")
        self.assertEqual(collector._TYPE_CODE_CACHE["A1B2C3"], "A321")
        self.assertEqual(self._row("A1B2C3"), ("N100EX", "positive", "A321"))

    def test_resolve_keeps_type_code_without_registration(self):
        resp = _hexdb_response({"Registration": "", "ICAOTypeCode": "C130"})
        with mock.patch.object(collector.requests, "get", return_value=resp):
            self.assertIsNone(collector.resolve_icao_to_tail("A1B2C3"))
        self.assertEqual(collector._TYPE_CODE_CACHE["A1B2C3"], "C130")
        self.assertEqual(self._row("A1B2C3"), (None, "negative", "C130"))

    def test_resolve_rejects_junk_type_code(self):
        resp = _hexdb_response({"Registration": "N100EX",
                                "ICAOTypeCode": "<b>x</b>",
                                "RegisteredOwners": "Example Air"})
        with mock.patch.object(collector.requests, "get", return_value=resp):
            collector.resolve_icao_to_tail("A1B2C3")
        self.assertEqual(collector._TYPE_CODE_CACHE["A1B2C3"], "")
        self.assertEqual(self._row("A1B2C3")[2], "")

    def test_404_caches_empty_type_code(self):
        with mock.patch.object(collector.requests, "get",
                               return_value=_hexdb_response({}, status=404)):
            collector.resolve_icao_to_tail("A1B2C3")
        self.assertEqual(self._row("A1B2C3"), (None, "negative", ""))

    def test_pre_v15_row_is_refetched_once(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO hexdb_cache (icao, registration, resolved_at, "
            "last_outcome, hit_count, registered_owner, type_code) "
            "VALUES ('A1B2C3','N100EX',?, 'positive',0,'Example Air',NULL)",
            (int(time.time()),))
        conn.commit()
        conn.close()
        resp = _hexdb_response({"Registration": "N100EX", "ICAOTypeCode": "A321",
                                "RegisteredOwners": "Example Air"})
        with mock.patch.object(collector.requests, "get",
                               return_value=resp) as get:
            collector.resolve_icao_to_tail("A1B2C3")
            _reset_caches()                      # simulate a restart
            collector.resolve_icao_to_tail("A1B2C3")
        self.assertEqual(get.call_count, 1)      # second call was a DB hit
        self.assertEqual(collector._TYPE_CODE_CACHE["A1B2C3"], "A321")


class TestTypeCodeMirror(_DbCase):
    """A hexdb type code fills a BLANK seen_aircraft type (typeless feeds,
    rows migration v16 blanked) — never a type the feed supplied."""

    def test_fills_blank_only_and_keeps_military(self):
        from categorize import classify
        now = int(time.time())
        conn = sqlite3.connect(self.db)
        try:
            conn.executemany(
                "INSERT INTO seen_aircraft (icao, first_seen_at, aircraft_type, "
                "aircraft_type_desc, category, fts_dirty) VALUES (?, ?, ?, '', ?, 0)",
                [("A1B2C3", now, "", "unknown"),
                 ("A1B2C4", now, "A20N", "commercial"),
                 ("A1B2C5", now, "", "military")])
            for icao in ("A1B2C3", "A1B2C4", "A1B2C5"):
                collector._hexdb_cache_write(conn, icao, "N100EX", "positive", now,
                                             registered_owner="Example Air",
                                             type_code="A321")
            rows = dict((r[0], r[1:]) for r in conn.execute(
                "SELECT icao, aircraft_type, category, fts_dirty FROM seen_aircraft"))
        finally:
            conn.close()
        self.assertEqual(rows["A1B2C3"], ("A321", classify("A321", "", False), 1))
        self.assertEqual(rows["A1B2C4"][:2], ("A20N", "commercial"))  # feed type kept
        self.assertEqual(rows["A1B2C5"][:2], ("A321", "military"))


class TestWarmTypeCodes(_DbCase):

    def test_loads_cached_codes_and_queues_the_rest(self):
        now = int(time.time())
        conn = sqlite3.connect(self.db)
        try:
            collector._hexdb_cache_write(conn, "A1B2C3", "N100EX", "positive", now,
                                         registered_owner="Example Air",
                                         type_code="A321")
            conn.commit()
            raw_list = [
                {"hex": "a1b2c3", "type": "adsb_icao"},       # cached in DB
                {"hex": "a1b2c4", "type": "mlat"},            # unknown → queue
                {"hex": "a1b2c5", "t": "B738"},               # feed has a type
                {"hex": "~a1b2c6", "type": "tisb_trackfile"},  # non-ICAO address
            ]
            with mock.patch.object(hexdb_resolver, "enqueue",
                                   return_value=True) as enq, \
                    mock.patch.object(hexdb_resolver, "start_worker") as start:
                collector._warm_type_codes(conn, raw_list)
            self.assertEqual(collector._TYPE_CODE_CACHE, {"A1B2C3": "A321"})
            enq.assert_called_once_with("A1B2C4")
            start.assert_called_once()
            self.assertEqual(collector.normalize(raw_list[0])["aircraft_type"], "A321")
        finally:
            conn.close()

    def test_nothing_to_do_when_feed_carries_types(self):
        conn = sqlite3.connect(self.db)
        try:
            with mock.patch.object(hexdb_resolver, "enqueue") as enq, \
                    mock.patch.object(hexdb_resolver, "start_worker") as start:
                collector._warm_type_codes(conn, [{"hex": "a1b2c3", "t": "A320"}])
            enq.assert_not_called()
            start.assert_not_called()
        finally:
            conn.close()


class _StopLoop(Exception):
    pass


class TestResolverWorkerRetry(unittest.TestCase):
    """A transient hexdb failure must not pin an ICAO in the seen-set for
    the rest of the process life (its next sighting re-enqueues it)."""

    def tearDown(self):
        _reset_caches()
        with hexdb_resolver._seen_lock:
            hexdb_resolver._seen.difference_update({"A1B2C7", "A1B2C8"})

    def _run_one(self, icao, resolve):
        with hexdb_resolver._seen_lock:
            hexdb_resolver._seen.add(icao)
        with mock.patch.object(hexdb_resolver, "_next_icao", return_value=icao), \
                mock.patch.object(collector, "resolve_icao_to_tail", side_effect=resolve), \
                mock.patch.object(hexdb_resolver.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                hexdb_resolver._worker_loop()

    def test_transient_error_forgets_icao(self):
        self._run_one("A1B2C7", lambda icao: None)   # nothing cached
        self.assertNotIn("A1B2C7", hexdb_resolver._seen)

    def test_answered_icao_stays_seen(self):
        def resolve(icao):
            collector._ICAO_CACHE[icao] = None       # negative answer cached
        self._run_one("A1B2C8", resolve)
        self.assertIn("A1B2C8", hexdb_resolver._seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
