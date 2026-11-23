"""A stop doesn't wait on a request that is still running (3.4.199).

uvicorn used to finish every in-flight request before it handed the stop signal
on, with no limit: a slow backup download kept a stopping Aerodrome alive, and
collecting, for minutes (measured on a Raspberry Pi), and `main.py restart` then
found it still running and started nothing. main.serve() gives such requests
GRACEFUL_STOP_SEC. This runs the real web server in a subprocess with a response
that never ends and a client that stops reading, then stops it.

Skipped when AERODROME_SKIP_SLOW_TESTS=1.
"""
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKIP_SLOW = os.environ.get("AERODROME_SKIP_SLOW_TESTS") == "1"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipIf(SKIP_SLOW, "AERODROME_SKIP_SLOW_TESTS=1 (subprocess web server)")
class TestAStopDoesNotWaitOnASlowRequest(unittest.TestCase):

    def test_the_web_server_stops_within_its_limit_while_a_download_is_still_running(self):
        sys.path.insert(0, str(REPO))
        import main
        port = _free_port()
        code = textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, {str(REPO)!r})
            import main
            from fastapi import FastAPI
            from fastapi.responses import StreamingResponse
            app = FastAPI()

            @app.get("/big")
            def big():
                chunk = b"x" * 65536
                return StreamingResponse(chunk for _ in range(10 ** 9))

            print("ready", flush=True)
            main.serve(app, "127.0.0.1", {port})
            """)
        server = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=str(REPO))
        self.addCleanup(lambda: (server.kill(), server.wait()))
        server.stdout.readline()
        client = None
        for _ in range(100):
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        self.assertIsNotNone(client, "the web server never listened")
        self.addCleanup(client.close)
        client.sendall(b"GET /big HTTP/1.1\r\nHost: test\r\n\r\n")
        self.assertIn(b"200", client.recv(65536))
        # The client reads no more: the server fills the socket buffers and its
        # response can't finish. Now stop it.
        time.sleep(1)
        t0 = time.monotonic()
        server.send_signal(signal.SIGTERM)
        limit = main.GRACEFUL_STOP_SEC + 4
        try:
            server.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            self.fail(f"still running {limit} s after the stop signal, waiting on the download")
        self.assertLess(time.monotonic() - t0, limit)


if __name__ == "__main__":
    unittest.main()
