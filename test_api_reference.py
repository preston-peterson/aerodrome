"""API.md is the API reference (v3.4.189): FastAPI's interactive /docs and /redoc pages are
off (they load their scripts from an outside CDN), /openapi.json stays, every route and every
query parameter the app declares is in API.md, and the in-app Documentation page serves it."""
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

import collector
import server
from schema_migrations import apply_schema_migrations

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")   # get_app() below must never reach GitHub

REPO = Path(__file__).resolve().parent


class TestApiReference(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        db = str(cls.dir / "history.db")
        collector.init_db(db)
        conn = sqlite3.connect(db)
        apply_schema_migrations(conn, "test")
        conn.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = db
        cfg["watchlist"] = []
        (cls.dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        cls.app = server.get_app(cfg, str(cls.dir / "config.yaml"))
        cls.spec = cls.app.openapi()
        cls.doc = (REPO / "API.md").read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def blocks_for(self, path):
        """API.md's table rows and paragraphs that name `path` in backticks (a query string after it is fine)."""
        out = []
        for block in re.split(r"\n(?=\|)|\n\n", self.doc):
            if re.search(r"`(?:[A-Z]+ )?" + re.escape(path) + r"(?:[`?])", block):
                out.append(block)
        return out

    def test_no_interactive_pages_but_the_description_stays(self):
        paths = {getattr(r, "path", "") for r in self.app.routes}
        for gone in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            self.assertNotIn(gone, paths)
        self.assertIn("/openapi.json", paths)
        self.assertGreater(len(self.spec["paths"]), 90)

    def test_every_route_and_query_parameter_is_in_api_md(self):
        missing = []
        for path, ops in self.spec["paths"].items():
            blocks = self.blocks_for(path)
            if not blocks:
                missing.append(path)
                continue
            text = "\n".join(blocks)
            for method, op in ops.items():
                for p in op.get("parameters", []):
                    if p["in"] == "query" and not re.search(r"\b" + re.escape(p["name"]) + r"\b", text):
                        missing.append(f"{method.upper()} {path} ?{p['name']}")
        self.assertEqual(missing, [], "add these to API.md")

    def test_the_documentation_page_serves_it(self):
        ep = next(r.endpoint for r in self.app.routes if getattr(r, "path", "") == "/api/docs/{slug}")
        out = ep(slug="api")
        body = out.body.decode() if hasattr(out, "body") else str(out)
        self.assertTrue(body.startswith("# HTTP API reference"))
        self.assertIn("{ slug: 'api',", (REPO / "templates" / "docs.html").read_text())


if __name__ == "__main__":
    unittest.main()
