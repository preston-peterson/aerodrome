"""v3.4.140 web-origin hardening: the DNS-rebinding Host allowlist, the
sudoers recovery command, squawk/photo-link sanitising, and the
web.allowed_hosts validator. Pure-function tests — no server, no network.
Hostnames are invented (example.com / example.net per RFC 2606).
"""
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

import collector
import photo_resolver
import server
from config_validator import validate_config, LIVE_KEYS, requires_restart

REPO = Path(__file__).resolve().parent


def _req(host, accept="application/json", server_addr=("192.0.2.10", 8000)):
    headers = [(b"accept", accept.encode())]
    if host is not None:
        headers.append((b"host", host.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "query_string": b"", "server": server_addr})


class TestHostAllowed(unittest.TestCase):

    def ok(self, host, allowed="", own="kiosk-a"):
        return server._host_allowed(host, allowed, own)

    def test_ip_literals_always_allowed(self):
        for h in ("192.0.2.10", "192.0.2.10:8000", "127.0.0.1:8000",
                  "[::1]:8000", "[2001:db8::5]", "::1"):
            with self.subTest(h=h):
                self.assertTrue(self.ok(h))

    def test_local_names_always_allowed(self):
        for h in ("localhost:8000", "kiosk-a:8000", "kiosk-a", "tracker",
                  "raspberrypi.local:8000", "box.lan", "box.home.arpa",
                  "box.internal", "box.localdomain", "KIOSK-A.LOCAL"):
            with self.subTest(h=h):
                self.assertTrue(self.ok(h))

    def test_public_names_refused_unless_listed(self):
        self.assertFalse(self.ok("rebind.example.com:8000"))
        self.assertFalse(self.ok("aerodrome.example.net"))
        listed = "aerodrome.example.net, other.example.org"
        self.assertTrue(self.ok("aerodrome.example.net:8000", listed))
        self.assertTrue(self.ok("AERODROME.EXAMPLE.NET.", listed))   # case + trailing dot
        self.assertFalse(self.ok("rebind.example.com", listed))

    def test_own_hostname_even_if_dotted(self):
        self.assertTrue(self.ok("kiosk-a.example.com", own="kiosk-a.example.com"))

    def test_missing_host_is_not_a_browser(self):
        self.assertTrue(self.ok(""))

    def test_notifications_public_url_host_is_trusted(self):
        cfg = {"web": {"allowed_hosts": "a.example.com"},
               "notifications": {"public_url": "https://tracker.example.net:8443/x"}}
        hosts = server._configured_hosts(cfg)
        self.assertTrue(self.ok("tracker.example.net:8443", hosts))
        self.assertTrue(self.ok("a.example.com", hosts))
        self.assertFalse(self.ok("rebind.example.com", hosts))
        self.assertEqual(server._configured_hosts({}), "")
        self.assertEqual(server._configured_hosts(
            {"notifications": {"public_url": "http://[bad"}}), "")


class TestHostRejectedResponse(unittest.TestCase):

    def test_api_call_gets_json(self):
        r = server._host_rejected_response(_req("rebind.example.com:8000"))
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'"host_not_allowed"', r.body)

    def test_browser_gets_page_with_link_and_escaped_host(self):
        r = server._host_rejected_response(
            _req('evil.example.com"><script>x()</script>', accept="text/html"))
        body = r.body.decode()
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("<script>x()", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn('href="http://192.0.2.10:8000/config#web"', body)

    def test_wildcard_bind_gets_no_bogus_link(self):
        r = server._host_rejected_response(
            _req("rebind.example.com", accept="text/html", server_addr=("0.0.0.0", 8000)))
        self.assertNotIn("http://0.0.0.0", r.body.decode())


class TestAllowedHostsConfig(unittest.TestCase):

    def _cfg(self, allowed):
        return {"receiver": {"ip": "192.0.2.10", "port": 8080, "path": "/data/aircraft.json",
                             "poll_interval": 60},
                "web": {"host": "0.0.0.0", "port": 8000, "allowed_hosts": allowed},
                "data": {"db_file": "x.db"}, "retention": {"military_days": 30,
                "watchlist_days": 30, "all_days": 30}}

    def _errs(self, allowed):
        return [e for e in validate_config(self._cfg(allowed)) if e[0] == "web.allowed_hosts"]

    def test_valid_lists(self):
        for v in ("", "aerodrome.example.com", "a.example.com, b.example.net ,"):
            with self.subTest(v=v):
                self.assertEqual(self._errs(v), [])

    def test_rejects_urls_ports_and_non_strings(self):
        for v in ("http://aerodrome.example.com", "aerodrome.example.com:8000", ["x"]):
            with self.subTest(v=v):
                self.assertTrue(self._errs(v))

    def test_live_key_no_restart(self):
        self.assertIn("web.allowed_hosts", LIVE_KEYS)
        self.assertFalse(requires_restart(["web.allowed_hosts"]))


class TestSudoersRecoveryCommand(unittest.TestCase):

    def test_points_at_staged_recover_script_with_install_dir(self):
        cmd = server._sudoers_recovery_command(Path("/opt/aerodrome/update/aerodrome"),
                                               Path("/opt/aerodrome"))
        self.assertEqual(cmd, "sudo bash /opt/aerodrome/update/aerodrome/scripts/recover-sudoers.sh "
                              "--install-dir /opt/aerodrome")
        self.assertNotIn("install.sh", cmd)
        self.assertNotIn("AERODROME_INSTALL_DIR=", cmd)   # no `sudo VAR=value` (needs SETENV)

    def test_paths_with_spaces_are_quoted(self):
        cmd = server._sudoers_recovery_command(Path("/home/u/My Apps/aero/update/aerodrome"),
                                               Path("/home/u/My Apps/aero"))
        self.assertIn("--install-dir '/home/u/My Apps/aero'", cmd)
        self.assertIn("'/home/u/My Apps/aero/update/aerodrome/scripts/recover-sudoers.sh'", cmd)
        self.assertNotIn("install.sh", cmd)

    def test_live_command_is_the_installed_recover_script(self):
        self.assertEqual(server._live_recovery_command(Path("/home/u/My Apps/aero")),
                         "sudo bash '/home/u/My Apps/aero/scripts/recover-sudoers.sh'")

    def test_target_script_ships_and_has_no_staging_refusal(self):
        script = (REPO / "scripts" / "recover-sudoers.sh").read_text()
        self.assertIn("AERODROME_INSTALL_DIR", script)    # the v3.4.140–144 command still works
        self.assertIn("--install-dir", script)
        # install.sh's guard is what made the old command fail (exit 2)
        self.assertNotIn("sitting inside an existing install's update/", script)
        self.assertIn("sitting inside an existing install's update/",
                      (REPO / "install.sh").read_text())


class TestSquawkAndPhotoSanitising(unittest.TestCase):

    def test_non_digit_squawk_dropped_at_ingest(self):
        for raw, want in (("7700", "7700"), (1200, "1200"), ("12", "0012"),
                          ("<!--", ""), ("7a00", ""), (None, "")):
            with self.subTest(raw=raw):
                self.assertEqual(collector._normalize_squawk(raw), want)
                self.assertEqual(server._normalize_squawk_str(raw), want)

    def test_photo_urls_https_only(self):
        resp = mock.Mock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"photos": [{
            "thumbnail_large": {"src": "https://cdn.example.com/t.jpg"},
            "link": "javascript:alert(1)", "photographer": "Example"}]}
        with mock.patch.object(photo_resolver.requests, "get", return_value=resp):
            got = photo_resolver._fetch("A1B2C3")
        self.assertEqual(got["thumbnail_url"], "https://cdn.example.com/t.jpg")
        self.assertEqual(got["photo_link"], "")
        resp.json.return_value["photos"][0]["thumbnail_large"]["src"] = "http://cdn.example.com/t.jpg"
        with mock.patch.object(photo_resolver.requests, "get", return_value=resp):
            self.assertIsNone(photo_resolver._fetch("A1B2C3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
