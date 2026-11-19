"""The Diagnostics hub is gone: Performance is the only diagnostic, so
/diagnostics is a redirect for old bookmarks and the hub, watchlist-alert and
slow-query diagnostic pages — plus the on-demand EXPLAIN and connection-tuning
endpoints only that page used — are no longer registered. The slow-query ring
endpoints the Performance page reads (/recent, /clear) still are.
"""
import os
import unittest

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

from test_destructive_endpoints import _AppCase, _endpoint


class TestDiagnosticsTrim(_AppCase):

    def routes(self):
        return {(getattr(r, "path", ""), m)
                for r in self.app.routes
                for m in (getattr(r, "methods", None) or ())}

    def test_the_four_trimmed_routes_are_gone(self):
        live = self.routes()
        for path in ("/diagnostics/watchlist-alerts", "/diagnostics/slow-queries",
                     "/api/diagnostics/slow-queries/explain",
                     "/api/diagnostics/connection-tuning-audit"):
            self.assertNotIn((path, "GET"), live, path)

    def test_the_ring_endpoints_the_performance_page_uses_stay(self):
        live = self.routes()
        self.assertIn(("/api/diagnostics/slow-queries/recent", "GET"), live)
        self.assertIn(("/api/diagnostics/slow-queries/clear", "POST"), live)

    def test_diagnostics_redirects_to_performance(self):
        response = _endpoint(self.app, "/diagnostics", "GET")()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/performance")


if __name__ == "__main__":
    unittest.main()
