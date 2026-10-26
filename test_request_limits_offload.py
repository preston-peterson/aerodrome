"""Update paths off the event loop, and the update-zip unpacked-size cap.

An `async def` handler that does blocking work (unzipping, downloading,
hashing, writing files) runs ON the event loop, so while it works every
other request — the Live page, /api/status, the Updates page's own polling —
waits. The update upload, the GitHub apply and config import are plain
`def` handlers now (run in the threadpool). Two static guards keep it that
way: the set of `async def` route handlers is an explicit allowlist, and
nothing awaits a plain function (v3.4.51 shipped `await apply_local_update()`
after that function became sync, and every GitHub apply failed).

The size cap: an update zip's declared unpacked size is checked before
anything is decompressed or update/ is wiped.

Requests are driven through the real get_app() ASGI stack; installs are
throwaway directories. Invented data only.
"""
import ast
import asyncio
import io
import json
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from test_update_apply import (NEW, REPO, _EndpointCase, _release_files, _status, _upload,
                               _zip_release, repo_guard)

setUpModule, tearDownModule = repo_guard()

# The only route handlers allowed to be `async def`. Each either awaits real
# async I/O or is a thin shell that stays on the loop on purpose; B4c removes
# backup_import and restore_from_path.
ASYNC_HANDLER_ALLOWLIST = {
    "get_resolve_tail", "enrich_aircraft_registry", "get_callsign_route",
    "get_aircraft_photo", "switch_to_real_execute", "backup_import", "restore_from_path",
}


def _multipart(field, filename, data, ctype="application/octet-stream"):
    b = "----aerodromeB4aBoundary"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() \
        + data + f"\r\n--{b}--\r\n".encode()
    return body, [(b"content-type", f"multipart/form-data; boundary={b}".encode()),
                  (b"content-length", str(len(body)).encode())]


async def _asgi(app, method, path, body=b"", headers=(), query=b""):
    msgs = []
    done = asyncio.Event()
    sent = {"body": False}

    async def receive():
        if not sent["body"]:
            sent["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(m):
        msgs.append(m)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query, "root_path": "",
             "headers": [(b"host", b"127.0.0.1:18299"), *headers],
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 18299)}
    try:
        await app(scope, receive, send)
    finally:
        done.set()
    status = next(m["status"] for m in msgs if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")
    return status, data


async def _with_ticker(coro):
    """Run `coro` beside a 10 ms ticker; return (result, worst loop stall)."""
    loop = asyncio.get_running_loop()
    state = {"done": False, "lag": 0.0}

    async def tick():
        while not state["done"]:
            t0 = loop.time()
            await asyncio.sleep(0.01)
            state["lag"] = max(state["lag"], loop.time() - t0 - 0.01)
    tk = asyncio.create_task(tick())
    await asyncio.sleep(0.03)
    try:
        return await coro, state["lag"]
    finally:
        state["done"] = True
        await tk


def _slow(fn, sec=0.4):
    def wrapper(*a, **kw):
        time.sleep(sec)
        return fn(*a, **kw)
    return wrapper


class TestOffLoop(_EndpointCase):
    """The blocking primitive inside each handler is slowed by 0.4 s; a 10 ms
    ticker on the loop must never stall more than 0.2 s."""

    MAX_STALL = 0.2

    def run_request(self, *a, **kw):
        return asyncio.run(_with_ticker(_asgi(self.app, *a, **kw)))

    def test_update_upload(self):
        data = _zip_release(_release_files("3.4.160"), wrapper="aerodrome-v3.4.160")
        body, hdrs = _multipart("file", "aerodrome-v3.4.160.zip", data)
        with mock.patch.object(zipfile.ZipFile, "testzip", _slow(zipfile.ZipFile.testzip)):
            (status, raw), lag = self.run_request("POST", "/api/updates/local/upload", body, hdrs)
        self.assertEqual(status, 200, raw[:300])
        self.assertLess(lag, self.MAX_STALL, f"the loop stalled {lag:.2f}s")

    def test_github_apply(self):
        import sqlite3
        import urllib.error
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO update_state (id, last_check_ts, last_check_result, "
                  "last_known_latest, last_known_latest_ts, updated_at) "
                  "VALUES (1, 1, 'success', ?, 1, 1)", ("v" + NEW,))
        c.commit()
        c.close()

        def unreachable(*a, **kw):
            time.sleep(0.4)
            raise urllib.error.URLError("example: network unreachable")
        with mock.patch("urllib.request.urlopen", unreachable):
            (status, raw), lag = self.run_request("POST", "/api/updates/github/apply")
        self.assertEqual(status, 500, raw[:300])
        self.assertIn(b"Couldn't reach GitHub", raw)
        self.assertLess(lag, self.MAX_STALL, f"the loop stalled {lag:.2f}s")

    def test_config_import(self):
        import yaml
        text = (self.tmp / "config-live.yaml").read_bytes()
        body, hdrs = _multipart("file", "config.yaml", text, "application/x-yaml")
        with mock.patch.object(yaml, "safe_load", _slow(yaml.safe_load)):
            (status, raw), lag = self.run_request("POST", "/api/config/import", body, hdrs)
        self.assertEqual(status, 200, raw[:300])
        self.assertLess(lag, self.MAX_STALL, f"the loop stalled {lag:.2f}s")


class TestNoLoopBlockingHandlers(unittest.TestCase):

    def setUp(self):
        self.tree = ast.parse((REPO / "server.py").read_text())

    def test_async_route_handlers_are_the_allowlist(self):
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for d in node.decorator_list:
                    f = d.func if isinstance(d, ast.Call) else d
                    if isinstance(f, ast.Attribute) and f.attr in (
                            "get", "post", "put", "delete", "patch") \
                            and getattr(f.value, "id", "") == "app":
                        found.add(node.name)
        self.assertEqual(found, ASYNC_HANDLER_ALLOWLIST,
                         "a new `async def` route handler runs on the event loop — make it "
                         "a plain def (or add it here only if it never blocks)")

    def test_nothing_awaits_a_plain_function(self):
        plain = {n.name for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)}
        coros = {n.name for n in ast.walk(self.tree) if isinstance(n, ast.AsyncFunctionDef)}
        bad = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name):
                name = node.value.func.id
                if name in plain and name not in coros:
                    bad.append(f"line {node.lineno}: await {name}(…)")
        self.assertEqual(bad, [])


def _bomb(size=201 * 1024 * 1024):
    """A ~200 KB zip whose one entry declares (and inflates to) >200 MB."""
    buf = io.BytesIO()
    chunk = b"\0" * (1024 * 1024)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        with z.open("aerodrome-v3.4.199/VERSION.pad", "w", force_zip64=True) as f:
            for _ in range(size // len(chunk)):
                f.write(chunk)
        z.writestr("aerodrome-v3.4.199/VERSION", "3.4.199\n")
        z.writestr("aerodrome-v3.4.199/main.py", "#\n")
    return buf.getvalue()


class TestUnpackedSizeCap(_EndpointCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bomb = _bomb()

    def test_bomb_is_small_but_inflates_past_the_cap(self):
        self.assertLess(len(self.bomb), 1024 * 1024)
        with zipfile.ZipFile(io.BytesIO(self.bomb)) as z:
            self.assertGreater(sum(i.file_size for i in z.infolist()),
                               self.server.UPDATE_UNPACKED_MAX_BYTES)

    def test_upload_gets_413_and_keeps_the_staged_release(self):
        before = self.update_snapshot()
        with mock.patch.object(zipfile.ZipFile, "testzip",
                               side_effect=AssertionError("decompressed the bomb")):
            r = self.ep("/api/updates/local/upload")(file=_upload(self.bomb))
        self.assertEqual(_status(r), 413)
        b = json.loads(r.body)
        self.assertEqual(b["code"], "too_large")
        self.assertEqual(b["error"], b["message"])
        self.assertEqual(self.update_snapshot(), before)

    def test_upload_gets_507_when_it_wouldnt_fit(self):
        small = _zip_release(_release_files("3.4.160"), wrapper="aerodrome-v3.4.160")
        before = self.update_snapshot()
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024 * 1024)):
            r = self.ep("/api/updates/local/upload")(file=_upload(small))
        self.assertEqual(_status(r), 507)
        self.assertEqual(self.update_snapshot(), before)

    def test_github_stage_gets_413_before_the_wipe(self):
        import hashlib
        import sqlite3
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO update_state (id, last_check_ts, last_check_result, "
                  "last_known_latest, last_known_latest_ts, updated_at) "
                  "VALUES (1, 1, 'success', 'v3.4.199', 1, 1)")
        c.commit()
        c.close()
        assets = {"aerodrome-v3.4.199.zip": self.bomb,
                  "aerodrome-v3.4.199.zip.sha256":
                      f"{hashlib.sha256(self.bomb).hexdigest()}  x\n".encode()}

        def urlopen(req, timeout=None):
            m = mock.MagicMock()
            m.read.return_value = assets[req.full_url.rsplit("/", 1)[1]]
            m.__enter__.return_value = m
            return m
        before = self.update_snapshot()
        with mock.patch("urllib.request.urlopen", urlopen):
            r = self.ep("/api/updates/github/apply")(snapshot_db=False)
        self.assertEqual(_status(r), 413, r.body[:300])
        self.assertEqual(self.update_snapshot(), before)
        self.restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
