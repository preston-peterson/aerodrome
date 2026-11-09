"""v3.4.179: the owner line uses the receiver's own aircraft database.

readsb run with --db-file sends each aircraft's registration (`r`) and its
registered owner (`ownOp`, the legal owner of record: often a bank trustee on an
airliner). hexdb's name is usually the operator, and hexdb knows nothing about
some aircraft. The collector keeps the feed's owner per aircraft (schema v20's
seen_aircraft.feed_owner) and fills an empty registration from the feed;
search.owner_names builds the owner line: hexdb's name, else the feed's, plus
"Registered to" with the feed's name when it names someone else.
"""
import asyncio
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import yaml

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

import collector
import server
from schema_migrations import CURRENT_SCHEMA_VERSION, _migration_v20_feed_owner, apply_schema_migrations
from search import detail_for_aircraft, owner_names
from test_collector_state import PLANE, _PollCase


class TestFeedFields(unittest.TestCase):

    def test_registration_is_cleaned(self):
        for raw, want in (("n308br ", "N308BR"), ("C-GABC", "C-GABC"), ("", ""), (None, ""),
                          (12345, ""), ("<b>x</b>", ""), ("N" * 11, ""), ("-ABC", "")):
            with self.subTest(raw=raw):
                self.assertEqual(collector.feed_registration({"r": raw}), want)

    def test_owner_is_cleaned(self):
        self.assertEqual(collector.feed_owner({"ownOp": "  EXAMPLE BANK NA\tTRUSTEE,  EXAMPLE NAVY\n"}),
                         "EXAMPLE BANK NA TRUSTEE, EXAMPLE NAVY")
        self.assertEqual(collector.feed_owner({"ownOp": "EXAMPLE\x00 AIR\x1b"}), "EXAMPLE AIR")
        self.assertEqual(collector.feed_owner({"ownOp": 7}), "")
        self.assertEqual(collector.feed_owner({}), "")
        self.assertEqual(len(collector.feed_owner({"ownOp": "X" * 500})), 120)

    def test_a_non_string_value_is_dropped_before_normalize(self):
        ac = collector.normalize(collector._sanitize_feed_record(dict(PLANE, r=["N1"], ownOp={"a": 1})))
        self.assertEqual((ac["registration"], ac["feed_owner"]), ("", ""))


class TestOwnerNames(unittest.TestCase):

    def test_the_owner_line(self):
        cases = [
            # hexdb's name, the feed's -> (owner, registered_to)
            ("Example Connection", "EXAMPLE AIR LINES INC", ("Example Connection", "EXAMPLE AIR LINES INC")),
            ("Example Air Lines", "EXAMPLE AIR LINES INC, DEPT 595 AIRCRAFT REGISTRATIONS",
             ("Example Air Lines", "")),
            ("Example Airlines", "EXAMPLE AIRLINES INC", ("Example Airlines", "")),
            ("Example Air", "EXAMPLE BANK NA TRUSTEE", ("Example Air", "EXAMPLE BANK NA TRUSTEE")),
            ("", "EXAMPLE FLYING CLUB INC", ("EXAMPLE FLYING CLUB INC", "")),
            ("Example Air", "", ("Example Air", "")),
            (None, None, ("", "")),
            ("The Example Co.", "EXAMPLE CORP", ("The Example Co.", "")),
        ]
        for looked_up, feed, want in cases:
            with self.subTest(looked_up=looked_up, feed=feed):
                self.assertEqual(owner_names(looked_up, feed), want)


class TestMigration(_PollCase):

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def cols(self):
        return {r[1] for r in self.q("PRAGMA table_info(seen_aircraft)")}

    def test_a_fresh_install_has_the_column(self):
        self.assertIn("feed_owner", self.cols())
        self.assertEqual(self.q("SELECT MAX(version) FROM schema_version")[0][0], CURRENT_SCHEMA_VERSION)

    def test_an_upgrade_from_v19_adds_it_and_keeps_rows(self):
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE seen_aircraft DROP COLUMN feed_owner")
        conn.execute("DELETE FROM schema_version WHERE version = 20")
        conn.execute("INSERT INTO seen_aircraft (icao, first_seen_at, registered_owner) "
                     "VALUES ('A00001', 1, 'Example Air')")
        conn.commit()
        self.assertNotIn("feed_owner", self.cols())
        result = apply_schema_migrations(conn, "test")
        conn.close()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual((result["starting_version"], result["ending_version"]), (19, 20))
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(self.q("SELECT icao, registered_owner, feed_owner FROM seen_aircraft"),
                         [("A00001", "Example Air", None)])

    def test_a_rerun_is_a_no_op(self):
        conn = sqlite3.connect(self.db)
        _migration_v20_feed_owner(conn)
        _migration_v20_feed_owner(conn)
        conn.close()
        self.assertIn("feed_owner", self.cols())


class TestWritePath(_PollCase):

    def row(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT registration, feed_owner FROM seen_aircraft "
                                "WHERE icao = 'A00001'").fetchone()
        finally:
            conn.close()

    def fts_finds(self, term):
        conn = sqlite3.connect(self.db)
        try:
            return bool(conn.execute("SELECT 1 FROM seen_aircraft_fts WHERE seen_aircraft_fts MATCH ?",
                                     (f'"{term}"',)).fetchall())
        finally:
            conn.close()

    def test_the_feed_fields_are_stored(self):
        self.poll([dict(PLANE, r="n123ex", ownOp="EXAMPLE BANK NA TRUSTEE")])
        self.assertEqual(self.row(), ("N123EX", "EXAMPLE BANK NA TRUSTEE"))

    def test_a_feed_without_them_stores_nothing(self):
        self.poll([PLANE])
        self.assertEqual(self.row(), (None, None))

    def test_the_feed_registration_fills_an_empty_one_and_search_finds_it(self):
        self.poll([PLANE])
        self.assertFalse(self.fts_finds("N123EX"))
        self.poll([dict(PLANE, r="N123EX")])
        self.assertEqual(self.row()[0], "N123EX")
        self.assertTrue(self.fts_finds("N123EX"))

    def test_hexdbs_registration_is_kept(self):
        self.poll([PLANE])
        conn = sqlite3.connect(self.db)
        collector._hexdb_cache_write(conn, "A00001", "N1HEX", "positive", 1794000000,
                                     registered_owner="Example Air")
        conn.commit()
        conn.close()
        self.poll([dict(PLANE, r="N2FEED")])
        self.assertEqual(self.row()[0], "N1HEX")

    def test_the_owner_follows_the_feed_and_a_blank_keeps_it(self):
        self.poll([dict(PLANE, ownOp="EXAMPLE LEASING LLC")])
        self.poll([dict(PLANE, ownOp="EXAMPLE BANK NA TRUSTEE")])
        self.assertEqual(self.row()[1], "EXAMPLE BANK NA TRUSTEE")
        self.poll([dict(PLANE, ownOp="")])
        self.poll([PLANE])
        self.assertEqual(self.row()[1], "EXAMPLE BANK NA TRUSTEE")


class TestReadPath(_PollCase):

    def setUp(self):
        super().setUp()
        cfg_path = Path(self.db).parent / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))
        self.app = server.get_app(self.cfg, str(cfg_path))

    def seed(self, registered_owner, feed_owner, registration="N123EX"):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO seen_aircraft (icao, first_seen_at, last_seen_at, registration, "
                     "registered_owner, feed_owner) VALUES ('A00001', 1, 1, ?, ?, ?)",
                     (registration, registered_owner, feed_owner))
        conn.commit()
        conn.close()

    def detail(self):
        conn = sqlite3.connect(self.db)
        try:
            d = detail_for_aircraft(conn, "a00001")
        finally:
            conn.close()
        return d["owner"], d["registered_to"], d["feed_owner"]

    def enrich(self, resolve=None):
        ep = next(r.endpoint for r in self.app.routes
                  if getattr(r, "path", "") == "/api/aircraft/{icao}/enrich")
        with mock.patch.object(collector, "resolve_icao_to_tail", side_effect=resolve) as m:
            j = asyncio.run(ep("A00001"))
        return j, m.called

    def test_the_detail_shows_both_names_when_they_differ(self):
        self.seed("Example Connection", "EXAMPLE AIR LINES INC")
        self.assertEqual(self.detail(), ("Example Connection", "EXAMPLE AIR LINES INC", "EXAMPLE AIR LINES INC"))

    def test_the_detail_shows_one_name_when_they_match(self):
        self.seed("Example Airlines", "EXAMPLE AIRLINES INC")
        self.assertEqual(self.detail()[:2], ("Example Airlines", ""))

    def test_enrich_falls_back_to_the_feeds_owner_when_hexdb_has_none(self):
        self.seed(None, "EXAMPLE FLYING CLUB INC")
        j, asked = self.enrich()
        self.assertTrue(asked, "hexdb is still asked for its name")
        self.assertEqual((j["owner"], j["registered_to"], j["registration"], j["registered_owner"]),
                         ("EXAMPLE FLYING CLUB INC", "", "N123EX", ""))

    def test_enrich_adds_registered_to_once_hexdb_answers(self):
        self.seed(None, "EXAMPLE BANK NA TRUSTEE")

        def resolve(key):
            conn = sqlite3.connect(self.db)
            collector._hexdb_cache_write(conn, key, "N123EX", "positive", 1794000000,
                                         registered_owner="Example Air")
            conn.commit()
            conn.close()

        j, _ = self.enrich(resolve)
        self.assertEqual((j["owner"], j["registered_to"]), ("Example Air", "EXAMPLE BANK NA TRUSTEE"))

    def test_enrich_doesnt_ask_hexdb_again_once_it_has_a_name(self):
        self.seed("Example Air", "EXAMPLE BANK NA TRUSTEE")
        j, asked = self.enrich()
        self.assertFalse(asked)
        self.assertEqual(j["registered_to"], "EXAMPLE BANK NA TRUSTEE")


if __name__ == "__main__":
    unittest.main()
