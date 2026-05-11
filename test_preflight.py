"""
Test the v2.50.35 pre-flight bind validation.

The bind check in main._preflight_bind_check is a small, pure function
that takes (host, port) and returns (ok, error). It should:
  - Succeed on 0.0.0.0 (always bindable)
  - Succeed on 127.0.0.1 (loopback always works)
  - Fail on a non-local IP that isn't assigned to this machine
  - Return clear error text on failure

Run:
    python3 test_preflight.py
"""
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# main.py imports uvicorn at module load. In environments without
# uvicorn (some test sandboxes), skip — the tests run on real
# installs where uvicorn is always present.
try:
    from main import _preflight_bind_check
    _HAS_DEPS = True
except ModuleNotFoundError as e:
    _HAS_DEPS = False
    _MISSING = str(e)


@unittest.skipUnless(_HAS_DEPS, f"main.py dependencies missing: {_MISSING if not _HAS_DEPS else ''}")
class TestPreflightBindCheck(unittest.TestCase):

    def _free_port(self):
        """Get a port that's free on this machine."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_zero_dot_zero_succeeds(self):
        """0.0.0.0 (bind-all) should always pass the check."""
        port = self._free_port()
        ok, err = _preflight_bind_check("0.0.0.0", port)
        self.assertTrue(ok, f"0.0.0.0 should always bind; got error: {err}")
        self.assertEqual(err, "")

    def test_loopback_succeeds(self):
        """Loopback is always assigned, so this should always pass."""
        port = self._free_port()
        ok, err = _preflight_bind_check("127.0.0.1", port)
        self.assertTrue(ok, f"127.0.0.1 should always bind; got error: {err}")

    def test_unbindable_ip_fails(self):
        """An IP that doesn't belong to this machine should fail."""
        # 192.0.2.x is RFC 5737 TEST-NET-1 — guaranteed not to be a
        # real address, so binding to it always fails. Reliable test
        # value.
        ok, err = _preflight_bind_check("192.0.2.1", 8000)
        self.assertFalse(ok, "Bind to TEST-NET-1 should have failed")
        # The error should mention the host so users can match it
        self.assertIn("192.0.2.1", err)

    def test_error_message_actionable(self):
        """The returned error text should be human-readable enough that
        a user can map it back to a config issue."""
        ok, err = _preflight_bind_check("192.0.2.1", 8000)
        self.assertFalse(ok)
        # Should not be empty, should mention what went wrong
        self.assertGreater(len(err), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
