"""
Unit tests for photo_resolver.py (ICAO-hex→aircraft-photo enrichment).

All planespotters HTTP is mocked — no network. Exercises the cache-first
logic: parse+store a hit, serve a fresh hit/miss from cache without
re-fetching, negative-cache a "no photos" result (planespotters signals this
as HTTP 200 with an empty photo list, NOT a 404), NOT cache a transient error
(so it retries), reject an invalid hex without fetching, and re-fetch a stale
entry.
"""
import sqlite3
import time
import unittest
from unittest import mock

from schema_migrations import _migration_v13_photo_cache
import photo_resolver

HIT_PAYLOAD = {"photos": [{
    "id": "1691204",
    "thumbnail": {"src": "https://t.plnspttrs.net/x_t.jpg", "size": {"width": 200, "height": 133}},
    "thumbnail_large": {"src": "https://t.plnspttrs.net/x_280.jpg", "size": {"width": 419, "height": 280}},
    "link": "https://www.planespotters.net/photo/1691204/n776de?utm_source=api",
    "photographer": "OMGcat",
}]}
MISS_PAYLOAD = {"photos": []}


def _conn():
    c = sqlite3.connect(":memory:")
    _migration_v13_photo_cache(c)
    return c


def _resp(status, payload=None):
    m = mock.Mock()
    m.status_code = status
    m.json.return_value = payload or {}
    if status >= 400:
        m.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    else:
        m.raise_for_status.return_value = None
    return m


class TestPhotoResolver(unittest.TestCase):
    def setUp(self):
        photo_resolver._stats.update(
            attempts=0, hits=0, misses=0, errors=0, last_error=None)

    def test_hit_parses_and_caches(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(200, HIT_PAYLOAD)):
            r = photo_resolver.resolve_photo(c, "aa7fa1")  # lowercase input
        self.assertTrue(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(r["icao"], "AA7FA1")
        self.assertEqual(r["thumbnail_url"], "https://t.plnspttrs.net/x_280.jpg")
        self.assertEqual(r["photographer"], "OMGcat")
        self.assertIn("planespotters.net/photo", r["photo_link"])
        self.assertEqual(
            c.execute("SELECT last_outcome, photographer FROM photo_cache "
                      "WHERE icao='AA7FA1'").fetchone(),
            ("hit", "OMGcat"))

    def test_fresh_cache_hit_skips_fetch(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(200, HIT_PAYLOAD)) as g:
            photo_resolver.resolve_photo(c, "AA7FA1")
            r2 = photo_resolver.resolve_photo(c, "AA7FA1")
            self.assertEqual(g.call_count, 1)  # 2nd call served from cache
        self.assertTrue(r2["found"])
        self.assertTrue(r2["cached"])
        self.assertEqual(r2["thumbnail_url"], "https://t.plnspttrs.net/x_280.jpg")

    def test_no_photos_negative_caches(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(200, MISS_PAYLOAD)):
            r = photo_resolver.resolve_photo(c, "A1B2C3")
        self.assertFalse(r["found"])
        self.assertEqual(
            c.execute("SELECT last_outcome, thumbnail_url FROM photo_cache "
                      "WHERE icao='A1B2C3'").fetchone(),
            ("miss", None))

    def test_negative_cache_hit_skips_fetch(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(200, MISS_PAYLOAD)) as g:
            photo_resolver.resolve_photo(c, "A1B2C3")
            r2 = photo_resolver.resolve_photo(c, "A1B2C3")
            self.assertEqual(g.call_count, 1)
        self.assertFalse(r2["found"])
        self.assertTrue(r2["cached"])

    def test_transient_5xx_not_cached(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(503)):
            r = photo_resolver.resolve_photo(c, "ABCDEF")
        self.assertFalse(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM photo_cache").fetchone()[0], 0)

    def test_bad_useragent_400_not_cached(self):
        # planespotters 400s a generic UA — treat as transient (raise), don't cache.
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(400)):
            r = photo_resolver.resolve_photo(c, "ABCDEF")
        self.assertFalse(r["found"])
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM photo_cache").fetchone()[0], 0)

    def test_network_exception_not_cached(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get",
                               side_effect=RuntimeError("timeout")):
            r = photo_resolver.resolve_photo(c, "ABCDEF")
        self.assertFalse(r["found"])
        self.assertFalse(r["cached"])
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM photo_cache").fetchone()[0], 0)

    def test_invalid_hex_no_fetch(self):
        c = _conn()
        with mock.patch.object(photo_resolver.requests, "get") as g:
            for bad in ("~AC82EC", "GHIJKL", "12345", "", "AA7FA12"):
                r = photo_resolver.resolve_photo(c, bad)
                self.assertFalse(r["found"], bad)
            self.assertFalse(g.called)

    def test_stale_entry_refetches(self):
        c = _conn()
        old = int(time.time()) - 40 * 86400   # older than the 30-day positive TTL
        c.execute("INSERT INTO photo_cache (icao, thumbnail_url, photo_link, "
                  "photographer, resolved_at, last_outcome, hit_count) "
                  "VALUES ('AA7FA1','https://old/x.jpg','https://old','Old',?,'hit',1)",
                  (old,))
        c.commit()
        with mock.patch.object(photo_resolver.requests, "get",
                               return_value=_resp(200, HIT_PAYLOAD)) as g:
            r = photo_resolver.resolve_photo(c, "AA7FA1")
            self.assertTrue(g.called)                 # stale → refetched
        self.assertEqual(r["thumbnail_url"], "https://t.plnspttrs.net/x_280.jpg")  # fresh, not old


if __name__ == "__main__":
    unittest.main(verbosity=2)
