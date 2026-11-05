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
import os
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from test_update_apply import (NEW, REPO, _EndpointCase, _release_files, _status, _upload,
                               _zip_release, repo_guard)

os.environ.setdefault("AERODROME_NO_UPDATE_CHECK", "1")

setUpModule, tearDownModule = repo_guard()

# The only route handlers allowed to be `async def`. Each either awaits real
# async I/O or is a thin shell that stays on the loop on purpose. (v3.4.149:
# backup_import and restore_from_path are plain defs now.)
ASYNC_HANDLER_ALLOWLIST = {
    "get_resolve_tail", "enrich_aircraft_registry", "get_callsign_route",
    "get_aircraft_photo", "switch_to_real_execute",
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


# ===========================================================================
# B4b (v3.4.148): per-path body caps, one large upload at a time, the space
# check before an upload is read, and switch-to-real off the loop.
# ===========================================================================

MB = 1024 * 1024


class _Body:
    """An ASGI receive that serves `chunks`, counts the body bytes the app
    read, can hold before a given call, and then reports a disconnect."""

    def __init__(self, chunks=(), hold_at=None):
        self.chunks = list(chunks)
        self.read = 0
        self.calls = 0
        self.hold_at = hold_at
        self.hold = None

    async def __call__(self):
        self.calls += 1
        if self.hold_at is not None and self.calls == self.hold_at:
            self.hold = self.hold or asyncio.Event()
            await self.hold.wait()
            return {"type": "http.disconnect"}
        if self.chunks:
            c = self.chunks.pop(0)
            self.read += len(c)
            return {"type": "http.request", "body": c, "more_body": bool(self.chunks)}
        return {"type": "http.disconnect"}


def _scope(method, path, headers, host=b"127.0.0.1:18299"):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "",
            "headers": [(b"host", host), *headers],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 18299)}


async def _drive(app, method, path, receive, headers=(), host=b"127.0.0.1:18299"):
    """(status, json body) of one request through the full ASGI app."""
    msgs = []

    async def send(m):
        msgs.append(m)
    try:
        await app(_scope(method, path, list(headers), host), receive, send)
    except Exception:          # the client went away: no response to read
        pass
    start = next((m for m in msgs if m["type"] == "http.response.start"), None)
    data = b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")
    try:
        body = json.loads(data) if data else None
    except ValueError:
        body = {"_raw": data[:200]}
    return (start or {}).get("status"), body


def _mp_headers(clen=None, boundary="B4bBoundary"):
    h = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    if clen is not None:
        h.append((b"content-length", str(clen).encode()))
    return h


def _mp_chunks(total, chunk=64 * 1024, boundary="B4bBoundary"):
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"b.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode()
    out = [head]
    left = total
    while left > 0:
        n = min(chunk, left)
        out.append(b"\0" * n)
        left -= n
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return out


def _deleted_spool_fds():
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            t = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        n += "(deleted)" in t
    return n


class _LimitCase(_EndpointCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(self._gate_free)

    def _gate_locked(self):
        g = getattr(self.server, "_LARGE_UPLOAD", None)   # (absent before v3.4.148)
        return bool(g is not None and g.locked())

    def _gate_free(self):
        self.assertFalse(self._gate_locked(), "the large-upload gate leaked")

    def caps(self):
        """The middleware's per-route caps (an empty dict before v3.4.148)."""
        return getattr(self.server, "_UPLOAD_ROUTE_CAPS", {})

    def drive(self, method, path, receive, headers=(), host=b"127.0.0.1:18299", limit=30):
        async def go():
            return await asyncio.wait_for(
                _drive(self.app, method, path, receive, headers, host), limit)
        return asyncio.run(go())


class TestBodyCaps(_LimitCase):
    """Before v3.4.148 anything labelled multipart/form-data skipped the cap,
    and the upload routes only checked their own limits after Starlette had
    spooled the whole body."""

    def test_multipart_to_a_json_route_is_refused_before_reading(self):
        rx = _Body(_mp_chunks(3 * MB))
        status, body = self.drive("PUT", "/api/config", rx, _mp_headers(3 * MB + 300))
        self.assertEqual(status, 413)
        self.assertEqual(rx.read, 0)

    def test_chunked_multipart_to_a_json_route_stops_at_2_MB(self):
        rx = _Body(_mp_chunks(6 * MB))
        status, body = self.drive("PUT", "/api/config", rx, _mp_headers())
        self.assertEqual(status, 413)
        self.assertLessEqual(rx.read, 2 * MB + 64 * 1024 + 300)

    def test_config_import_over_256_KB_is_refused_before_reading(self):
        rx = _Body(_mp_chunks(400 * 1024))
        status, body = self.drive("POST", "/api/config/import", rx,
                                  _mp_headers(400 * 1024 + 300))
        self.assertEqual(status, 413)
        self.assertEqual(rx.read, 0)
        self.assertIn("256 KB", body["message"])
        self.assertEqual(body["error"], body["message"])

    def test_update_upload_over_50_MB_is_refused_before_reading(self):
        rx = _Body(_mp_chunks(1 * MB))
        status, body = self.drive("POST", "/api/updates/local/upload", rx,
                                  _mp_headers(60 * MB))
        self.assertEqual(status, 413)
        self.assertEqual(rx.read, 0)
        self.assertIn("50 MB", body["message"])

    def test_backup_upload_over_2_GB_is_refused_before_reading(self):
        rx = _Body(_mp_chunks(1 * MB))
        status, body = self.drive("POST", "/api/backup/import", rx,
                                  _mp_headers(3 * 1024 * MB))
        self.assertEqual(status, 413)
        self.assertEqual(rx.read, 0)
        self.assertIn("scp", body["message"])

    def test_backup_upload_past_its_cap_mid_stream_is_413_and_frees_the_spool(self):
        caps = dict(self.caps())
        caps["/api/backup/import"] = (1 * MB, "too big (test cap)")
        before = _deleted_spool_fds()
        with mock.patch.dict(self.caps(), caps):
            rx = _Body(_mp_chunks(4 * MB))
            status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers())
        self.assertEqual(status, 413)
        self.assertEqual(body["message"], "too big (test cap)")
        self.assertLessEqual(rx.read, 1 * MB + 64 * 1024 + 64 * 1024 + 300)
        self.assertLessEqual(_deleted_spool_fds(), before, "the spooled upload was left open")

    def test_a_small_json_body_still_reaches_the_route(self):
        # Guard: ordinary JSON requests are unaffected by the caps.
        rx = _Body([json.dumps({"kind": "update"}).encode()])
        status, body = self.drive("POST", "/api/maintenance/ack", rx,
                                  [(b"content-type", b"application/json")])
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])


class TestLargeUploadGate(_LimitCase):
    """C1-11: two 2 GB uploads at once (or one during a restore) spooled
    gigabytes before anything could refuse them. Refusals now come before
    the first body byte is read."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(self.server, "_spool_free_bytes", create=True,
                              return_value=100 * 1024 * MB)
        self.free = p.start()
        self.addCleanup(p.stop)

    def test_a_second_large_upload_is_refused_before_its_first_byte(self):
        async def go():
            first = _Body(_mp_chunks(1 * MB), hold_at=3)
            t1 = asyncio.create_task(_drive(self.app, "POST", "/api/backup/import", first,
                                            _mp_headers(2 * 1024 * MB)))
            while first.calls < 3:
                await asyncio.sleep(0.01)
            second = _Body(_mp_chunks(1 * MB))
            r2 = await _drive(self.app, "POST", "/api/backup/import", second,
                              _mp_headers(1024 * MB))
            first.hold.set()
            await t1
            return r2, second.read
        (status, body), read = asyncio.run(go())
        self.assertEqual(status, 409)
        self.assertEqual(read, 0)
        self.assertEqual(body["code"], "upload_in_progress")
        self.assertIn("Another upload is in progress", body["message"])

    def test_a_backup_upload_is_refused_while_maintenance_runs(self):
        tok = self.server._MAINT.try_acquire("a server-side backup")
        self.addCleanup(self.server._MAINT.release, tok)
        rx = _Body(_mp_chunks(1 * MB))
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(1 * MB + 300))
        self.assertEqual(status, 409)
        self.assertEqual(rx.read, 0)
        self.assertEqual(body["busy"]["op"], "a server-side backup")
        self.assertEqual(body["error"], body["message"])

    def test_an_update_upload_is_refused_while_the_last_update_is_confirmed(self):
        (self.inst / ".update-pending.json").write_text('{"state": "pending_first_boot"}')
        rx = _Body(_mp_chunks(1 * MB))
        status, body = self.drive("POST", "/api/updates/local/upload", rx,
                                  _mp_headers(1 * MB + 300))
        self.assertEqual(status, 409)
        self.assertEqual(rx.read, 0)
        self.assertEqual(body["code"], "update_pending")

    def test_a_declared_size_over_free_space_is_507(self):
        self.free.return_value = 300 * MB
        rx = _Body(_mp_chunks(1 * MB))
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(100 * MB))
        self.assertEqual(status, 507)
        self.assertEqual(rx.read, 0)
        self.assertIn("Not enough free disk space", body["message"])

    def test_a_chunked_body_crossing_the_free_space_line_is_507(self):
        seq = iter([10 * 1024 * MB] + [100 * MB] * 1000)
        self.free.side_effect = lambda: next(seq)
        with mock.patch.object(self.server, "_SPACE_RECHECK_BYTES", 256 * 1024, create=True):
            rx = _Body(_mp_chunks(8 * MB))
            status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers())
        self.assertEqual(status, 507)
        self.assertLess(rx.read, 1 * MB)
        self.assertEqual(body["code"], "no_space")

    def test_the_gate_is_free_after_a_413_a_409_and_a_disconnect(self):
        # Guard of the new gate: it never stays taken.
        caps = dict(self.caps())
        caps["/api/backup/import"] = (1 * MB, "too big")
        with mock.patch.dict(self.caps(), caps):
            self.drive("POST", "/api/backup/import", _Body(_mp_chunks(4 * MB)), _mp_headers())
        self.assertFalse(self._gate_locked())
        # a client that goes away mid-body (the user cancelled)
        cancel = _Body(_mp_chunks(3 * MB)[:5])
        self.drive("POST", "/api/backup/import", cancel, _mp_headers(3 * MB + 300))
        self.assertFalse(self._gate_locked())
        tok = self.server._MAINT.try_acquire("a restore")
        self.drive("POST", "/api/backup/import", _Body(_mp_chunks(1 * MB)),
                   _mp_headers(1 * MB + 300))
        self.server._MAINT.release(tok)
        self.assertFalse(self._gate_locked())


class _Stalled:
    """A sender that sends `chunks` and then vanishes without closing the
    connection (lid closed, Wi-Fi gone): receive() never returns again."""

    def __init__(self, chunks, gap=0.0, complete=False):
        self.chunks, self.gap, self.read, self.complete = list(chunks), gap, 0, complete

    async def __call__(self):
        if self.chunks:
            if self.gap:
                await asyncio.sleep(self.gap)
            c = self.chunks.pop(0)
            self.read += len(c)
            more = bool(self.chunks) or not self.complete
            return {"type": "http.request", "body": c, "more_body": more}
        await asyncio.Event().wait()


class TestUploadThatStopsArriving(_LimitCase):
    """Before, nothing timed a body out: a sender that vanished held the
    one-large-upload gate (and its spool) until the service restarted, and
    every other restore or update upload got 409 meanwhile."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(self.server, "_spool_free_bytes", create=True,
                                    return_value=100 * 1024 * MB),
                  mock.patch.object(self.server, "UPLOAD_STALL_TIMEOUT_SEC", 0.3, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_stalled_upload_is_ended_and_frees_the_gate_and_its_spool(self):
        before = _deleted_spool_fds()
        rx = _Stalled(_mp_chunks(2 * MB)[:20])        # ~1.2 MB, then silence
        status, body = self.drive("POST", "/api/backup/import", rx,
                                  _mp_headers(300 * MB), limit=10)
        self.assertEqual(status, 408, body)
        self.assertEqual(body["code"], "upload_stalled")
        self.assertEqual(body["error"], body["message"])
        self.assertFalse(self._gate_locked())
        self.assertLessEqual(_deleted_spool_fds(), before, "the stalled spool was left open")
        # The next upload is not refused as "another upload is in progress".
        status, body = self.drive("POST", "/api/updates/local/upload", _Body(_mp_chunks(MB)),
                                  _mp_headers(MB + 300))
        self.assertNotEqual(status, 409, body)

    def test_a_slow_but_steady_upload_is_not_ended(self):
        # Guard: the deadline restarts with every chunk.
        chunks = _mp_chunks(256 * 1024)
        total = sum(len(c) for c in chunks)
        rx = _Stalled(chunks, gap=0.1, complete=True)  # 6 chunks, 0.1 s apart > 0.3 s total
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(total))
        self.assertEqual(rx.read, total)
        self.assertEqual(status, 400, body)            # reached the endpoint: not a zip
        self.assertIn("Not a valid zip", body["message"])

    def test_no_deadline_once_the_body_is_in(self):
        # Guard: once the whole body has arrived the app may wait on
        # receive() (for a disconnect) as long as it likes — a long restore
        # is still running, nothing is stalled.
        seen = []

        async def app(scope, receive, send):
            while (await receive()).get("more_body"):
                pass
            seen.append((await receive())["type"])      # waits past the stall limit
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        msgs = [{"type": "http.request", "body": b"x" * 1000, "more_body": True},
                {"type": "http.request", "body": b"", "more_body": False}]

        async def rx():
            if msgs:
                return msgs.pop(0)
            await asyncio.sleep(0.6)                    # > UPLOAD_STALL_TIMEOUT_SEC (0.3)
            return {"type": "http.disconnect"}
        mw = self.server._BodySizeLimitMiddleware(app)
        status, _body = asyncio.run(asyncio.wait_for(
            _drive(mw, "POST", "/api/backup/import", rx, _mp_headers(1000)), 10))
        self.assertEqual(seen, ["http.disconnect"])
        self.assertEqual(status, 200)


class TestRefusedHostOrOriginNeverReachesTheGate(_LimitCase):
    """The upload gate answered before the Host allowlist and the
    same-origin check: another site's page (DNS rebinding) could read what
    maintenance task was running and the free disk space. Those requests
    now get the guard's 403, body unread, gate untouched."""

    EVIL = b"evil.example.com"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(self.server, "_spool_free_bytes", create=True,
                              return_value=100 * 1024 * MB)
        self.free = p.start()
        self.addCleanup(p.stop)

    def assert_403_unread(self, status, body, rx):
        self.assertEqual(status, 403, body)
        self.assertEqual(rx.read, 0)
        self.assertNotIn("busy", body)
        self.assertNotIn("available", json.dumps(body))
        self.assertFalse(self._gate_locked())

    def test_while_maintenance_runs(self):
        tok = self.server._MAINT.try_acquire("a server-side backup")
        self.addCleanup(self.server._MAINT.release, tok)
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(MB + 300),
                                  host=self.EVIL)
        self.assert_403_unread(status, body, rx)
        self.assertEqual(body["error"], "host_not_allowed")

    def test_while_an_update_is_being_confirmed(self):
        (self.inst / ".update-pending.json").write_text('{"state": "pending_first_boot"}')
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/updates/local/upload", rx,
                                  _mp_headers(MB + 300), host=self.EVIL)
        self.assert_403_unread(status, body, rx)

    def test_when_the_disk_is_short(self):
        self.free.return_value = 300 * MB
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(100 * MB),
                                  host=self.EVIL)
        self.assert_403_unread(status, body, rx)

    def test_from_another_sites_page(self):
        tok = self.server._MAINT.try_acquire("a restore")
        self.addCleanup(self.server._MAINT.release, tok)
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/backup/import", rx,
                                  [*_mp_headers(MB + 300),
                                   (b"origin", b"http://attacker.example.com")])
        self.assert_403_unread(status, body, rx)
        self.assertEqual(body["error"], "Cross-origin request rejected.")

    def test_the_page_itself_still_gets_the_409(self):
        # Guard: a same-origin request still learns why it was refused.
        tok = self.server._MAINT.try_acquire("a restore")
        self.addCleanup(self.server._MAINT.release, tok)
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/backup/import", rx,
                                  [*_mp_headers(MB + 300),
                                   (b"origin", b"http://127.0.0.1:18299")])
        self.assertEqual(status, 409, body)
        self.assertEqual(rx.read, 0)


class TestRestoreUploadSpace(_LimitCase):
    """A restore upload needs room for its spool, its staged copy and what
    the restore itself checks for (its size again plus the database). The
    gate checked 1x, so uploads of about a third of the free space and up
    were received in full and only then refused (or failed with a raw
    "[Errno 28]" 500 while being staged)."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(self.server, "_spool_free_bytes", create=True,
                              return_value=1000 * MB)
        self.free = p.start()
        self.addCleanup(p.stop)

    def test_an_upload_that_needs_three_times_its_size_is_refused_unread(self):
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(300 * MB))
        self.assertEqual(status, 507, body)
        self.assertEqual(rx.read, 0)
        self.assertEqual(body["code"], "no_space")

    def test_an_update_upload_still_needs_only_its_size(self):
        # Guard: only the restore upload is multiplied (and a small restore
        # upload still gets through).
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/updates/local/upload", rx,
                                  _mp_headers(40 * MB))
        self.assertNotEqual(status, 507, body)
        self.assertGreater(rx.read, 0)
        rx = _Body(_mp_chunks(MB))
        status, body = self.drive("POST", "/api/backup/import", rx, _mp_headers(200 * MB))
        self.assertNotEqual(status, 507, body)
        self.assertGreater(rx.read, 0)

    def chunked(self, path, total, free):
        """A body without Content-Length (re-checked every 4 MB here)."""
        self.free.return_value = free
        rx = _Body(_mp_chunks(total, chunk=MB))
        with mock.patch.object(self.server, "_SPACE_RECHECK_BYTES", 4 * MB), \
                mock.patch.object(self.server, "_db_size_bytes", return_value=0):
            status, body = self.drive("POST", path, rx, _mp_headers())
        return status, body, rx

    def test_a_chunked_restore_upload_is_rechecked_for_three_times_its_size(self):
        # At 4 MB in, the restore still needs its staged copy and the restore
        # check (8 MB) plus the headroom (256 MB): 260 MB free is not enough,
        # although it is more than the headroom alone.
        before = _deleted_spool_fds()
        status, body, rx = self.chunked("/api/backup/import", 6 * MB, 260 * MB)
        self.assertEqual(status, 507, body)
        self.assertEqual(body["code"], "no_space")
        self.assertLess(rx.read, 6 * MB)
        self.assertLessEqual(_deleted_spool_fds(), before, "the refused spool is still open")

    def test_a_chunked_upload_with_room_gets_through(self):
        # Guard: plenty of room for a restore; an update upload is only its
        # own size (260 MB free is enough for it).
        status, body, rx = self.chunked("/api/backup/import", 6 * MB, 1000 * MB)
        self.assertEqual(status, 400, body)            # reached the endpoint: not a zip
        self.assertIn("Not a valid zip", body["message"])
        status, body, rx = self.chunked("/api/updates/local/upload", 6 * MB, 260 * MB)
        self.assertNotEqual(status, 507, body)
        self.assertGreater(rx.read, 5 * MB)

    def test_a_disk_that_fills_while_the_upload_is_spooled_is_507(self):
        # The disk fills from elsewhere while the upload is being received
        # (after the space check let it in): the spool's write fails with
        # ENOSPC. That is the disk, not an upload that didn't arrive in full.
        import errno
        import starlette.formparsers as fp
        base = fp.SpooledTemporaryFile

        class FillsUp(base):
            def write(self, b):
                if self.tell() + len(b) > 2 * MB:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return super().write(b)
        before = _deleted_spool_fds()
        chunks = _mp_chunks(4 * MB)
        total = sum(len(c) for c in chunks)
        with mock.patch.object(fp, "SpooledTemporaryFile", FillsUp):
            status, body = self.drive("POST", "/api/backup/import", _Body(chunks),
                                      _mp_headers(total))
        self.assertEqual(status, 507, body)
        self.assertEqual(body["code"], "no_space")
        self.assertEqual(body["error"], body["message"])
        self.assertIn("disk filled up", body["message"])
        self.assertLessEqual(_deleted_spool_fds(), before, "the spool is still open")

    def test_a_disk_that_fills_while_the_upload_is_staged_is_507_not_500(self):
        from starlette.datastructures import UploadFile
        import atomic_io

        class Full(io.BytesIO):
            def write(self, b):
                raise OSError(28, "No space left on device")
        staged = []

        def open_full(path):
            staged.append(Path(path))
            Path(path).write_bytes(b"")
            return Full()
        up = UploadFile(file=io.BytesIO(b"PK" + b"\0" * 4096), filename="b.zip")
        with mock.patch.object(atomic_io, "open_private", side_effect=open_full):
            r = self.ep("/api/backup/import")(file=up, background_tasks=None)   # a plain def
        self.assertEqual(r.status_code, 507)
        b = json.loads(r.body)
        self.assertEqual(b["code"], "no_space")
        self.assertEqual(b["error"], b["message"])
        self.assertFalse(any(p.exists() for p in staged))


def _parts(n_files, fields=0, size=1024, boundary="B4bBoundary"):
    out = b""
    for i in range(n_files):
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"b{i}.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode()
        out += b"\0" * size + b"\r\n"
    for i in range(fields):
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"f{i}\"\r\n\r\n").encode() + b"x" * size + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()


class TestRestoreUploadIsOneFile(_LimitCase):
    """FastAPI parsed the restore upload with Starlette's defaults: up to
    1000 file parts and 1000 fields, each up to 1 MB in memory — one request
    could hold gigabytes of RAM within the 2 GB cap."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(self.server, "_spool_free_bytes", create=True,
                              return_value=100 * 1024 * MB)
        p.start()
        self.addCleanup(p.stop)

    def post(self, body):
        return self.drive("POST", "/api/backup/import", _Body([body]), _mp_headers(len(body)))

    def uploads(self):
        d = self.inst / ".backups" / ".uploads"
        return list(d.iterdir()) if d.is_dir() else []

    def test_many_file_parts_are_refused(self):
        status, body = self.post(_parts(50))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "bad_upload")
        self.assertEqual(self.uploads(), [])

    def test_extra_fields_are_refused(self):
        status, body = self.post(_parts(1, fields=3))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "bad_upload")

    def test_no_file_part_is_a_clear_400(self):
        status, body = self.post(_parts(0) )
        self.assertEqual(status, 400, body)
        self.assertEqual(body["message"], "No backup file was uploaded.")

    def test_a_cancelled_upload_frees_its_spool_at_once(self):
        # The user cancels a big restore upload: the request ends without an
        # exception (as FastAPI's own form parsing did) and its partial spool
        # is closed right away, not at some later garbage collection.
        import gc
        chunks = _mp_chunks(3 * MB)[:-1]               # never finishes ...

        async def rx():
            if chunks:
                return {"type": "http.request", "body": chunks.pop(0), "more_body": True}
            return {"type": "http.disconnect"}         # ... the user cancelled
        raised = []

        async def go():
            async def send(m):
                pass
            try:
                await self.app(_scope("POST", "/api/backup/import", _mp_headers(4 * MB)),
                               rx, send)
            except BaseException as e:      # noqa: BLE001
                raised.append(type(e).__name__)
        gc.disable()
        self.addCleanup(gc.enable)
        before = _deleted_spool_fds()
        asyncio.run(go())
        self.assertEqual(raised, [])
        self.assertLessEqual(_deleted_spool_fds(), before, "the cancelled spool is still open")

    def test_one_file_reaches_the_restore(self):
        # Guard: the page's own upload (one part named "file").
        status, body = self.post(_parts(1))
        self.assertEqual(status, 400, body)
        self.assertIn("Not a valid zip", body["message"])


class TestGateAfterAnEndpointError(unittest.TestCase):

    def test_the_gate_is_free_after_the_app_raises(self):
        # Guard: only normal returns were covered (413, 409, disconnect).
        import server
        self.addCleanup(lambda: server._LARGE_UPLOAD.locked() and server._LARGE_UPLOAD.release())
        reached = []

        async def boom(scope, receive, send):
            await receive()
            reached.append(server._LARGE_UPLOAD.locked())
            raise RuntimeError("endpoint failed")
        mw = server._BodySizeLimitMiddleware(boom)
        rx = _Body(_mp_chunks(64 * 1024))
        with mock.patch.object(server, "_spool_free_bytes", return_value=100 * 1024 * MB):
            with self.assertRaises(RuntimeError):
                asyncio.run(mw(_scope("POST", "/api/backup/import", _mp_headers(200 * 1024)),
                               rx, lambda m: asyncio.sleep(0)))
        self.assertEqual(reached, [True])
        self.assertFalse(server._LARGE_UPLOAD.locked(), "the gate leaked after an endpoint error")


class TestSwitchToRealOffLoop(_EndpointCase):
    """The receiver probe (up to 5 s), systemctl and the file work used to
    run on the event loop: every other request waited."""

    MAX_STALL = 0.2

    def test_the_loop_keeps_ticking_during_the_probe(self):
        import requests
        self.server.CONFIG.setdefault("demo", {})["enabled"] = True
        body = json.dumps({"receiver_ip": "192.0.2.20", "receiver_port": 30047}).encode()

        def slow_get(*a, **kw):
            time.sleep(0.4)
            return mock.Mock(status_code=200)
        with mock.patch.object(requests, "get", slow_get):
            (status, raw), lag = asyncio.run(_with_ticker(_asgi(
                self.app, "POST", "/api/setup/switch-to-real", body,
                [(b"content-type", b"application/json"),
                 (b"content-length", str(len(body)).encode())])))
        self.assertEqual(status, 200, raw[:300])
        self.assertTrue(json.loads(raw)["ok"], raw[:300])
        self.assertLess(lag, self.MAX_STALL, f"the loop stalled {lag:.2f}s")

    def test_invalid_json_keeps_its_200_contract(self):
        # Guard: the wizard's existing 200 contract (test_destructive_endpoints).
        self.server.CONFIG.setdefault("demo", {})["enabled"] = True
        status, raw = asyncio.run(_asgi(self.app, "POST", "/api/setup/switch-to-real",
                                        b"{not json", [(b"content-type", b"application/json")]))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": False, "error": "Invalid JSON body.",
                                           "steps": []})


if __name__ == "__main__":
    unittest.main()
