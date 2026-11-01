"""v3.4.146 notification-server settings hardening.

The server address reaches ntfy's server.yml, which ntfy (running as root)
reads. These tests pin that every path that writes the file — the two
/api/ntfy endpoints, install(), update_config() and backup restore — accepts
only a plain http(s) address, renders every value JSON-quoted, carries over
only the settings Aerodrome manages, and never drops a setting someone added
by hand without being told to: the Notifications tab names them and asks
first (static checks on templates/config.html). Also: the stale-data note and
"Also delete cached messages" look at the message cache server.yml names.

v3.4.150: the writes are Aerodrome's system helper's. No root, no network:
`sudo -n <helper> …` goes through a mocked subprocess.run that runs the REAL
helper in-process on a fake root (test_aerodrome_helper's harness), so these
tests see the files root would write. Addresses are RFC 5737 / example.net,
paths under /srv/example are invented.
"""
import ast
import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests
import yaml

import collector
import ntfy_installer
import privhelper
import server
from schema_migrations import apply_schema_migrations
from test_aerodrome_helper import HelperHost, HelperSudo

REPO = Path(__file__).resolve().parent

# What v3.4.144 and earlier wrote (comments trimmed): plain cache-file and
# cache-duration, quoted base-url/listen-http/upstream-base-url.
LEGACY = '''# Aerodrome-managed ntfy config. Edit at your own risk — future
# upgrades via Aerodrome may overwrite this file.
# Auto-detected LAN IP. If your phone can't reach this URL (e.g.
# you're on a different network), change it to something reachable
base-url: "http://192.0.2.10:2586"
listen-http: "0.0.0.0:2586"
cache-file: /var/lib/ntfy/cache.db
cache-duration: 12h
behind-proxy: false
# iOS instant-push support. Self-hosted ntfy cannot wake iPhones
upstream-base-url: "https://ntfy.sh"
'''
MANAGED_KEYS = {"base-url", "listen-http", "cache-file", "cache-duration",
                "behind-proxy", "upstream-base-url"}
OLD_SERVER_WARNING = ("ntfy base_url in the restored config may reference the OLD server's "
                      "LAN IP. Check it in the Notifications tab if your phones can't "
                      "reach ntfy after restore.")
INJECTION = 'http://192.0.2.10:2586"\nauth-file: "/srv/example/user.db"\n#'
GOOD_URLS = ["http://192.0.2.10:2586", "https://ntfy.example.net", "http://host-a:2586",
             "http://[2001:db8::1]:2586", "https://ntfy.example.net/sub/path",
             "http://adsb_pi.lan:2586", "http://Pi-Host.local:2586"]
BAD_URLS = ["http://x\n", "http://x\r", 'http://x"', "http://x'", "http://x\\", "http://x y",
            "http://x\t", "file:///etc/passwd", "javascript:alert(1)", "http://", "http://x?q=1",
            "http://x#f", "http://user@x", "http://x:0", "http://x:65536", "http://" + "a" * 260,
            "http://[fe80::1%eth0]:2586", INJECTION]


def _ni(name):
    """Look up lazily so a missing function fails one test, not the module."""
    return getattr(ntfy_installer, name)


# ---------------------------------------------------------------------------
# The shared block: URL grammar, classification, rendering, YAML parity
# ---------------------------------------------------------------------------

class SharedBlockUrls(unittest.TestCase):

    def test_good_urls_pass(self):
        for u in GOOD_URLS:
            self.assertTrue(_ni("base_url_ok")(u), u)
            self.assertEqual(_ni("validate_base_url")(u), u)

    def test_bad_urls_fail_with_one_plain_message(self):
        for u in BAD_URLS:
            self.assertFalse(_ni("base_url_ok")(u), repr(u))
            with self.assertRaises(ValueError) as cm:
                _ni("validate_base_url")(u)
            self.assertEqual(str(cm.exception),
                             "Server address must look like http://host:port — "
                             "no spaces, quotes or extra parts")


class SharedBlockClassify(unittest.TestCase):

    def classify(self, text):
        return _ni("classify_server_yml")(text)

    def test_legacy_aerodrome_file_is_clean(self):
        c = self.classify(LEGACY)
        self.assertEqual((c["custom"], c["problems"]), ([], []))
        self.assertEqual((c["port"], c["bind"], c["base_url"], c["upstream"]),
                         (2586, "0.0.0.0", "http://192.0.2.10:2586", True))

    def test_render_round_trip_and_yaml_key_set(self):
        render = _ni("render_server_yml")
        for cf in (None, "/var/lib/ntfy/cache.db"):
            for up in (True, False):
                t = render(80, "127.0.0.1", "https://ntfy.example.net", up, cf)
                c = self.classify(t)
                self.assertEqual((c["custom"], c["problems"]), ([], []))
                self.assertEqual((c["port"], c["bind"], c["upstream"]), (80, "127.0.0.1", up))
                y = yaml.safe_load(t)
                self.assertEqual(set(y), {"base-url", "listen-http", "cache-duration", "behind-proxy"}
                                 | ({"cache-file"} if cf else set())
                                 | ({"upstream-base-url"} if up else set()))
                self.assertEqual(y["base-url"], "https://ntfy.example.net")

    def test_renderer_refuses_bad_values_even_if_caller_skips_validation(self):
        render = _ni("render_server_yml")
        for args in ((2586, "0.0.0.0", INJECTION, True),
                     (2586, "0.0.0.0\nx: 1", "http://x", True),
                     (0, "0.0.0.0", "http://x", True),
                     (True, "0.0.0.0", "http://x", True),
                     ("2586", "0.0.0.0", "http://x", True)):
            with self.assertRaises(ValueError, msg=repr(args)):
                render(*args)
        with self.assertRaises(ValueError):
            render(2586, "0.0.0.0", "http://x", True, cache_file="/srv/example/cache.db")

    def test_hand_edited_shapes_carry_over(self):
        # v3.4.144's readers lost the port on '...' and on trailing comments.
        shapes = {
            'base-url: https://ntfy.example.net\nlisten-http: ":2586"\n': ("https://ntfy.example.net", 2586),
            "base-url: 'https://ntfy.example.net'\nlisten-http: '0.0.0.0:2586'\n": ("https://ntfy.example.net", 2586),
            'base-url: "https://ntfy.example.net"  # phone URL\nlisten-http: "0.0.0.0:80"   # moved\n': ("https://ntfy.example.net", 80),
            'base-url: "http://adsb_pi.lan:2586"\nlisten-http: "0.0.0.0:2586"\n': ("http://adsb_pi.lan:2586", 2586),
            'base-url: http://host-a:2586\nlisten-http: 0.0.0.0:2586\n': ("http://host-a:2586", 2586),
            'base-url : "http://host-a:2586"\r\nlisten-http: "127.0.0.1:8080"\r\n': ("http://host-a:2586", 8080),
            # plain ':PORT' is the string ':2586' to YAML and to ntfy
            'base-url: http://host-a:2586\nlisten-http: :2586\n': ("http://host-a:2586", 2586),
        }
        for text, (url, port) in shapes.items():
            c = self.classify(text)
            self.assertEqual((c["custom"], c["problems"]), ([], []), text)
            self.assertEqual((c["base_url"], c["port"]), (url, port), text)

    def test_operator_values_are_custom_never_silently_reset(self):
        c = self.classify(LEGACY.replace("behind-proxy: false", "behind-proxy: true")
                          .replace("cache-duration: 12h", "cache-duration: 72h"))
        self.assertEqual(c["custom"], ["behind-proxy", "cache-duration"])
        c = self.classify(LEGACY + 'auth-file: "/srv/example/user.db"\nauth-default-access: "deny-all"\n')
        self.assertEqual(c["custom"], ["auth-default-access", "auth-file"])
        c = self.classify(LEGACY.replace("/var/lib/ntfy/cache.db", "/srv/example/cache.db"))
        self.assertEqual(c["custom"], ["cache-file"])
        c = self.classify(LEGACY.replace('"https://ntfy.sh"', '"https://push.example.net"'))
        self.assertEqual(c["custom"], ["upstream-base-url"])
        self.assertFalse(c["upstream"])
        c = self.classify(LEGACY + "web-push:\n  key: abc\n")
        self.assertEqual(c["custom"], ["web-push"])
        # Shapes the gap check found the prototype accepting as clean:
        # an indented continuation folds into cache-file's value in YAML...
        c = self.classify(LEGACY.replace("cache-file: /var/lib/ntfy/cache.db",
                                         "cache-file: /var/lib/ntfy/cache.db\n  /srv/example"))
        self.assertIn("cache-file", c["custom"])
        # ...and '.5' is a float, not the string '.5'.
        c = self.classify(LEGACY.replace("cache-duration: 12h", "cache-duration: .5"))
        self.assertEqual(c["custom"], ["cache-duration"])

    def test_invalid_managed_values_are_problems_never_localhost(self):
        for bad in ("http://[fe80::1%eth0]:2586", "http://x?q=1"):
            c = self.classify(LEGACY.replace("http://192.0.2.10:2586", bad))
            self.assertIn("base-url", c["problems"])
            self.assertIsNone(c["base_url"])
        c = self.classify(LEGACY.replace('"0.0.0.0:2586"', '"198.51.100.1:2586"'))
        self.assertIn("listen-http", c["problems"])
        self.assertIsNone(c["port"])
        c = self.classify(LEGACY.replace('base-url: "http://192.0.2.10:2586"\n', ""))
        self.assertIn("base-url", c["problems"])
        self.assertIsNone(c["base_url"])

    def test_injected_file_is_contained(self):
        c = self.classify('base-url: "' + INJECTION + '"\nlisten-http: "0.0.0.0:2586"\n')
        self.assertEqual(c["custom"], ["auth-file"])
        self.assertEqual(c["base_url"], "http://192.0.2.10:2586")   # the line's own scalar

    def test_duplicate_and_nested_managed_keys_are_problems(self):
        c = self.classify(LEGACY + 'base-url: "http://other:1"\n')
        self.assertIn("base-url", c["problems"])
        self.assertIsNone(c["base_url"])               # flagged values are never carried
        c = self.classify(LEGACY.replace('listen-http: "0.0.0.0:2586"', 'listen-http:\n  - ":2586"'))
        self.assertIn("listen-http", c["problems"])
        self.assertTrue(any(k.startswith("line ") for k in c["custom"]))

    def test_bom_and_one_leading_document_marker_read_as_yaml_does(self):
        for text in ("\ufeff" + LEGACY, "---\n" + LEGACY, "# c\n--- # start\n" + LEGACY,
                     "\ufeff---  \n" + LEGACY):
            c = self.classify(text)
            self.assertEqual((c["custom"], c["problems"]), ([], []), repr(text[:12]))
            self.assertEqual(c["base_url"], "http://192.0.2.10:2586")
            self.assertEqual(yaml.safe_load(text)["base-url"], c["base_url"])
        # A second document marker, or one after a key, is not the same file
        # to YAML (safe_load refuses it): someone's edit, named by line.
        c = self.classify("---\n---\n" + LEGACY)
        self.assertEqual(c["custom"], ["line 2"])
        c = self.classify(LEGACY + "---\n")
        self.assertEqual(c["custom"], ["line 12"])

    def test_a_tab_in_a_lines_indentation_is_custom(self):
        """YAML refuses a tab where indentation goes, even on a blank or
        comment line; the line is named and the managed values still carry."""
        for extra in ("\t", " \t", "\t ", "\t# c", "\tauth-file: x"):
            c = self.classify(LEGACY.replace("behind-proxy: false\n",
                                             "behind-proxy: false\n" + extra + "\n"))
            self.assertEqual((c["custom"], c["problems"]), (["line 10"], []), repr(extra))
            self.assertEqual(c["base_url"], "http://192.0.2.10:2586")
            with self.assertRaises(yaml.YAMLError):
                yaml.safe_load(LEGACY.replace("behind-proxy: false\n",
                                              "behind-proxy: false\n" + extra + "\n"))

    def test_long_values_take_linear_time(self):
        """A 256 KB run of digits used to backtrack for minutes in the
        number check; an over-long value is now refused up front, and the
        number check itself stays linear (it is copied into a root helper)."""
        run = "1" * 262000 + "z"
        for text in ("x: " + run + "\n", "base-url: " + run + "\n" + LEGACY,
                     "listen-http: " + "_" * 262000 + "z\n"):
            t0 = time.perf_counter()
            c = self.classify(text)
            self.assertLess(time.perf_counter() - t0, 0.5)
            self.assertTrue(c["custom"] or c["problems"])
        t0 = time.perf_counter()
        self.assertIsNone(ntfy_installer._TYPED_RE.match(run))
        self.assertIsNone(ntfy_installer._TYPED_RE.match("1" * 100000 + ".1" + "_" * 100000 + "z"))
        self.assertLess(time.perf_counter() - t0, 0.5)
        ok, _ = _ni("_yaml_scalar")("a" * 1025)
        self.assertFalse(ok)
        self.assertEqual(_ni("_yaml_scalar")("a" * 1024), (True, "a" * 1024))

    def test_names_only_never_values(self):
        c = self.classify(LEGACY + 'auth-file: "/srv/example/user.db"\nnot a key line\n')
        dumped = json.dumps(c)
        self.assertNotIn("/srv/example", dumped)
        self.assertIn("line 13", c["custom"])

    def test_block_is_stdlib_only_and_python38(self):
        """The block will be copied into a helper that runs under the system
        interpreter: it must parse as 3.8 and need nothing but re and json."""
        text = (REPO / "ntfy_installer.py").read_text()
        begin = text.index("# --- BEGIN shared ntfy-yml block")
        end = text.index("# --- END shared ntfy-yml block ---")
        block = text[begin:end]
        ast.parse(block, feature_version=(3, 8))
        import re as _re
        ns = {"re": _re, "json": json, "__builtins__": __builtins__}
        exec(compile(block, "shared-block", "exec"), ns)
        for name in ("base_url_ok", "classify_server_yml", "render_server_yml", "_yaml_scalar"):
            self.assertTrue(callable(ns.get(name)), name)
        self.assertEqual(text.count("# --- BEGIN shared ntfy-yml block"), 1)

    def test_the_helper_runs_the_same_block(self):
        """v3.4.150: the helper carries a byte-identical copy; what the app
        accepts is what root writes."""
        def block(text):
            return text[text.index("# --- BEGIN shared ntfy-yml block"):
                        text.index("# --- END shared ntfy-yml block ---")]
        self.assertEqual(block((REPO / "ntfy_installer.py").read_text()),
                         block((REPO / "scripts" / "aerodrome-helper").read_text()))

    def test_the_helpers_url_check_is_the_apps(self):
        from test_aerodrome_helper import load_helper
        h = load_helper()
        for u in GOOD_URLS:
            self.assertEqual(h.v_base_url(u), u)
        for u in BAD_URLS:
            with self.assertRaises(Exception, msg=repr(u)):
                h.v_base_url(u)


SCALARS = ['"a"', "'a'", "a", '"a" # c', "'it''s'", "a # c", "true", "false", "False", "~", "",
           '"\\u00e9"', '"a\\tb"', "12h", "0.0.0.0:2586", ":2586", "2586", "yes", "no", "on",
           "1:30", "0x1F", "1e3", ".inf", "[a, b]", "{a: 1}", "&x a", "*x", "!!str a", "|", ">",
           '"unterminated', "'unterminated", '"a" b', "'a' b", "a: b", "- a", "a#b", "http://h:1/p",
           '"\\x41"', '"\\N"', "@a", "`a", "%a", "? a",
           # gap-check findings: tabs, '.NN' floats, quoted-then-'#' without a space
           "a\tb", "\ta", "a\t# c", ".55", ".5e+3", '"a"#c', "'a'#c", "Null", "NULL",
           "2026-10-21", "2026-10-21 12:00:00", "<<", "=",
           # leading ':' (plain ':PORT' is a string), number spellings
           "::2586", ":a", ":", ": a", ":#", ":2586 # c", "_1", "1_000", "1.5_0", "-.5", "+1e3",
           "1" * 2000, "1" * 2000 + "z", "a" * 1100]


class ParityWithPyYAML(unittest.TestCase):
    """Wherever the line parser says ok, its value is exactly PyYAML's."""

    def check(self, raw):
        ok, val = _ni("_yaml_scalar")(raw)
        if not ok:
            return
        try:
            y = yaml.safe_load("k: " + raw + "\n")
        except yaml.YAMLError as e:
            self.fail(f"{raw!r}: parser ok, PyYAML error {e}")
        self.assertIsInstance(y, dict, repr(raw))
        self.assertEqual(set(y), {"k"}, repr(raw))
        self.assertEqual((type(val), val), (type(y["k"]), y["k"]), repr(raw))

    def test_listed_shapes(self):
        for raw in SCALARS:
            self.check(raw)

    def test_seeded_fuzz(self):
        alpha = list("a0.:#'\" -_\t/xeE+~![]{},&*|>?%@`\\nNyYoO12h=<") + [" #", "\\u00e9"]
        rng = random.Random(146)
        for _ in range(4000):
            self.check("".join(rng.choice(alpha) for _ in range(rng.randint(1, 7))))

    def test_clean_files_read_the_same_in_pyyaml(self):
        """A file classify calls clean (no custom, no problems) is a mapping
        of managed keys whose values equal what classify reports. Every
        generated file has a usable base-url and listen-http, so a good share
        is clean and actually compared (asserted, so this can't go vacuous)."""
        keys = ["base-url", "listen-http", "upstream-base-url", "cache-file",
                "cache-duration", "behind-proxy", "auth-file"]
        vals = ['"http://192.0.2.10:2586"', "http://host-a:2586", "'https://ntfy.example.net'",
                '"0.0.0.0:2586"', "0.0.0.0:80", '"https://ntfy.sh"', "https://ntfy.sh",
                "/var/lib/ntfy/cache.db", "12h", "false", "False", "", '"x" # c', "1:30", ":2586"]
        urls = ['"http://192.0.2.10:2586"', "http://host-a:2586", "'https://ntfy.example.net'"]
        listens = ['"0.0.0.0:2586"', "0.0.0.0:80", "127.0.0.1:2586", ":2586", "':8080'"]
        extra = ["  - x", "  continued", "# comment", "", "---", "\tx: 1", "k:v",
                 "\t", " \t", "\t# c", "...", "--- # c"]
        rng = random.Random(1460)
        classify = _ni("classify_server_yml")
        clean = 0
        for _ in range(3000):
            lines = [rng.choice(extra) if rng.random() < 0.2
                     else rng.choice(keys) + ": " + rng.choice(vals)
                     for _ in range(rng.randint(0, 5))]
            for line in ("base-url: " + rng.choice(urls), "listen-http: " + rng.choice(listens)):
                lines.insert(rng.randint(0, len(lines)), line)
            if rng.random() < 0.1:
                lines.insert(0, rng.choice(["---", "\ufeff", "\ufeff---"]))
            text = "\n".join(lines) + "\n"
            c = classify(text)
            if c["custom"] or c["problems"]:
                continue
            clean += 1
            try:
                y = yaml.safe_load(text)
            except yaml.YAMLError as e:
                self.fail(f"classify: clean, PyYAML: {e}\n{text!r}")
            self.assertIsInstance(y, dict, text)
            self.assertLessEqual(set(y), MANAGED_KEYS, text)
            self.assertEqual(y["base-url"], c["base_url"], text)
            self.assertEqual(y["listen-http"].rsplit(":", 1),
                             [c["bind"] if y["listen-http"][0] != ":" else "",
                              str(c["port"])], text)
            self.assertEqual(y.get("upstream-base-url") == "https://ntfy.sh", c["upstream"], text)
        self.assertGreater(clean, 300)


# ---------------------------------------------------------------------------
# v3.4.150: every write goes through Aerodrome's system helper. The tests run
# the REAL helper in-process on a fake root (test_aerodrome_helper's
# HelperHost/HelperSudo): `sudo -n <helper> …` from privhelper lands there,
# and what ends up in the fake root is what root would have written.
# ---------------------------------------------------------------------------

class _NtfyCase(unittest.TestCase):
    """A managed install (still on the older root-run unit, running) whose
    files live in a fake root; the helper runs there."""

    STATE = "aerodrome_managed"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fake = self.tmp / "root"
        self.host = HelperHost(fake)
        self.cfg = fake / "etc" / "ntfy" / "server.yml"
        self.unit = fake / "etc" / "systemd" / "system" / "ntfy.service"
        self.stamp = fake / "var" / "lib" / "ntfy" / "aerodrome-installed"
        if self.STATE == "aerodrome_managed":
            self.host.legacy_host()
            self.cfg.unlink()              # each test writes the server.yml it needs
        self.sudo = HelperSudo(self.host, privhelper.HELPER)
        paths = {   # every module path constant lives under the fake root
            "BINARY_PATH": fake / "usr" / "local" / "bin" / "ntfy",
            "CONFIG_DIR": self.cfg.parent, "CONFIG_FILE": self.cfg,
            "SYSTEMD_UNIT": self.unit,
            "STAMP_FILE": self.stamp, "STAMP_DIR": self.stamp.parent,
            "VENDOR_PATHS": tuple(fake / p.lstrip("/") for p in ntfy_installer.NTFY_VENDOR_PATHS),
        }
        patches = [mock.patch.object(ntfy_installer, k, v) for k, v in paths.items()]
        patches += [
            mock.patch.object(ntfy_installer, "install_status",
                              side_effect=lambda: {"state": self.STATE, "arch": "linux_amd64"}),
            mock.patch.object(ntfy_installer, "latest_version", return_value="2.11.0"),
            mock.patch.object(ntfy_installer.subprocess, "run", side_effect=self.sudo),
            mock.patch.object(requests, "get", side_effect=AssertionError("network")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def live(self, text):
        self.cfg.write_text(text)

    def helper_calls(self):
        return self.sudo.helper_calls()


def config_args(port=2586, bind="0.0.0.0", base_url="http://192.0.2.10:2586", upstream=True,
                drop=None):
    """The ntfy-config call update_config makes."""
    return (["ntfy-config", "--port", str(port), "--bind", bind, "--base-url", base_url,
             "--upstream", "1" if upstream else "0"]
            + (["--drop-custom-keys", ",".join(drop)] if drop else []))


class UpdateConfig(_NtfyCase):

    def test_custom_keys_refuse_without_any_helper_call(self):
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        before = self.cfg.read_text()
        r = ntfy_installer.update_config(base_url="http://192.0.2.20:2586")
        self.assertFalse(r["ok"])
        self.assertEqual(r.get("code"), "custom_keys")
        self.assertEqual(r["custom_keys"], ["auth-file"])
        self.assertIn("auth-file", r["message"])
        self.assertNotIn("/srv/example", json.dumps(r))
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(self.cfg.read_text(), before)

    def test_drop_custom_keys_rewrites_managed_keys_only_then_restarts(self):
        live = LEGACY + 'auth-file: "/srv/example/user.db"\n'
        self.live(live)
        r = ntfy_installer.update_config(upstream_relay=False, drop_custom_keys=["auth-file"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["dropped_keys"], ["auth-file"])
        self.assertEqual(self.helper_calls(), [config_args(upstream=False, drop=["auth-file"])])
        self.assertIn(["systemctl", "restart", "ntfy"], self.host.calls)
        c = ntfy_installer.classify_server_yml(self.cfg.read_text())
        self.assertEqual((c["custom"], c["problems"]), ([], []))
        self.assertEqual((c["base_url"], c["port"], c["bind"], c["upstream"]),
                         ("http://192.0.2.10:2586", 2586, "0.0.0.0", False))
        self.assertEqual(set(yaml.safe_load(self.cfg.read_text())),
                         MANAGED_KEYS - {"upstream-base-url", "cache-file"})
        # saving moved it to the sandboxed unit; the file it changed is kept
        self.assertEqual(self.unit.read_text(), ntfy_installer.NTFY_UNIT_TEXT)
        pre = self.cfg.parent / "server.yml.pre-v6"
        self.assertEqual((pre.read_text(), stat.S_IMODE(pre.stat().st_mode)), (live, 0o600))

    def test_drop_needs_every_live_name(self):
        """drop_custom_keys confirms names, not a blanket drop: a setting the
        caller didn't name (added after the 409 it saw) refuses again."""
        live = (LEGACY + 'auth-file: "/srv/example/user.db"\n'
                'auth-default-access: "deny-all"\n')
        self.live(live)
        for confirm in (["auth-file"], [], None, True, "auth-file,auth-default-access"):
            r = ntfy_installer.update_config(base_url="http://192.0.2.20:2586",
                                             drop_custom_keys=confirm)
            self.assertEqual((r["ok"], r.get("code")), (False, "custom_keys"), confirm)
            self.assertEqual(r["custom_keys"], ["auth-default-access", "auth-file"])
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(self.cfg.read_text(), live)
        # Extra names (a setting already removed by hand) are harmless.
        r = ntfy_installer.update_config(
            base_url="http://192.0.2.20:2586",
            drop_custom_keys=["auth-default-access", "auth-file", "web-push-file"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["dropped_keys"], ["auth-default-access", "auth-file"])
        self.assertEqual(self.helper_calls()[-1][-1], "auth-default-access,auth-file")

    def test_root_checks_the_names_again(self):
        """The helper re-reads the file as root: a setting added between the
        app's read and the helper's is refused there (exit 3) and comes back
        as the same custom_keys answer, with nothing written."""
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        real_main = self.host.h.main

        def racing_main(argv, out=None):
            self.cfg.write_text(self.cfg.read_text() + 'auth-default-access: "deny-all"\n')
            self.host.h.main = real_main
            return real_main(argv, out=out)
        self.host.h.main = racing_main
        before_unit = self.unit.read_text()
        r = ntfy_installer.update_config(upstream_relay=False, drop_custom_keys=["auth-file"])
        self.assertEqual((r["ok"], r.get("code"), r.get("custom_keys")),
                         (False, "custom_keys", ["auth-default-access", "auth-file"]))
        self.assertIn("deny-all", self.cfg.read_text())
        self.assertEqual(self.unit.read_text(), before_unit)

    def test_clean_file_saves_without_any_confirmation(self):
        self.live(LEGACY)
        r = ntfy_installer.update_config(upstream_relay=False)
        self.assertEqual((r["ok"], r["dropped_keys"]), (True, []))
        self.assertEqual(r["message"], "ntfy config updated and service restarted.")

    def test_missing_file_is_unreadable_not_reset_to_defaults(self):
        """v3.4.144 re-rendered a missing server.yml from defaults."""
        r = ntfy_installer.update_config(upstream_relay=False)
        self.assertEqual((r["ok"], r.get("code")), (False, "unreadable"))
        self.assertEqual(self.sudo.calls, [])
        self.assertFalse(self.cfg.exists())

    def test_bad_port_or_bind_is_invalid_before_the_helper(self):
        self.live(LEGACY)
        for kw in ({"port": 0}, {"port": 65536}, {"bind": "192.0.2.1"}, {"bind": "0.0.0.0\nx: 1"},
                   {"base_url": INJECTION}):
            r = ntfy_installer.update_config(**kw)
            self.assertEqual((r["ok"], r.get("code")), (False, "invalid"), kw)
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(self.cfg.read_text(), LEGACY)

    def test_unreadable_upstream_line_is_refused_by_name(self):
        self.live(LEGACY + 'upstream-base-url: "https://ntfy.sh"\n')     # duplicated
        r = ntfy_installer.update_config(base_url="http://192.0.2.20:2586")
        self.assertEqual((r["ok"], r.get("code"), r.get("keys")),
                         (False, "stored_value_invalid", ["upstream-base-url"]))
        self.assertIn("can't carry over: upstream-base-url.", r["message"])
        self.assertNotIn("listen-http only", r["message"])
        self.assertEqual(self.sudo.calls, [])
        # Saying what the relay should be is the way out.
        r = ntfy_installer.update_config(base_url="http://192.0.2.20:2586", upstream_relay=True)
        self.assertTrue(r["ok"], r)

    def test_stored_invalid_address_is_refused_not_reset(self):
        self.live(LEGACY.replace("http://192.0.2.10:2586", "http://[fe80::1%eth0]:2586"))
        r = ntfy_installer.update_config(upstream_relay=False)
        self.assertEqual((r["ok"], r.get("code"), r.get("keys")),
                         (False, "stored_value_invalid", ["base-url"]))
        self.assertEqual(r["message"], "Your saved notification server address is no "
                                       "longer accepted — re-enter it.")
        self.assertEqual(self.sudo.calls, [])
        # Re-entering the address is the way out.
        r = ntfy_installer.update_config(base_url="http://192.0.2.10:2586", upstream_relay=False)
        self.assertTrue(r["ok"], r)

    def test_stored_invalid_listen_address_is_refused(self):
        self.live(LEGACY.replace('"0.0.0.0:2586"', '"198.51.100.1:2586"'))
        r = ntfy_installer.update_config(base_url="http://192.0.2.20:2586")
        self.assertEqual((r["ok"], r.get("code"), r.get("keys")),
                         (False, "stored_value_invalid", ["listen-http"]))
        self.assertEqual(r["message"], (
            "The notification server's config file has a value Aerodrome can't carry "
            "over: listen-http. Aerodrome keeps listen-http only as 0.0.0.0:PORT, "
            f"127.0.0.1:PORT or :PORT. Fix it in {self.cfg}, or uninstall and "
            "reinstall the notification server."))
        self.assertEqual(self.sudo.calls, [])

    def test_supplied_port_never_silently_replaces_a_hand_set_listen_address(self):
        """IPv6, host-name or one-interface binds Aerodrome can't represent:
        a supplied port/bind (restore) is refused rather than widening it."""
        for live in ('"[::1]:2586"', '"192.0.2.5:2586"', '"localhost:2586"', "localhost:2586"):
            self.live(LEGACY.replace('"0.0.0.0:2586"', live))
            r = ntfy_installer.update_config(base_url="http://192.0.2.10:2586",
                                             upstream_relay=True, port=2586, bind="0.0.0.0")
            self.assertEqual((r["ok"], r.get("code"), r.get("keys")),
                             (False, "stored_value_invalid", ["listen-http"]), live)
            self.assertEqual(self.sudo.calls, [])
        # A listen address Aerodrome wrote is replaced as before.
        self.live(LEGACY.replace('"0.0.0.0:2586"', '"127.0.0.1:2586"'))
        r = ntfy_installer.update_config(port=2586, bind="0.0.0.0")
        self.assertTrue(r["ok"], r)
        self.assertEqual(yaml.safe_load(self.cfg.read_text())["listen-http"], "0.0.0.0:2586")

    def test_hand_edited_quoting_keeps_port_and_address(self):
        """v3.4.144 read listen-http: '0.0.0.0:80' as no port and rewrote 2586."""
        self.live("base-url: 'https://ntfy.example.net'   # phone URL\n"
                  "listen-http: '0.0.0.0:80'  # moved off 2586\n"
                  "cache-file: /var/lib/ntfy/cache.db\ncache-duration: 12h\n"
                  "behind-proxy: false\nupstream-base-url: https://ntfy.sh\n")
        r = ntfy_installer.update_config(upstream_relay=False)
        self.assertTrue(r["ok"], r)
        y = yaml.safe_load(self.cfg.read_text())
        self.assertEqual(y["listen-http"], "0.0.0.0:80")
        self.assertEqual(y["base-url"], "https://ntfy.example.net")
        self.assertNotIn("upstream-base-url", y)

    def test_legacy_file_toggle_renders_exactly(self):
        self.live(LEGACY)
        r = ntfy_installer.update_config(upstream_relay=False)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.cfg.read_text(), ntfy_installer.render_server_yml(
            2586, "0.0.0.0", "http://192.0.2.10:2586", False))

    def test_injection_payload_is_refused_before_the_helper(self):
        self.live(LEGACY)
        r = ntfy_installer.update_config(base_url=INJECTION)
        self.assertFalse(r["ok"])
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(self.cfg.read_text(), LEGACY)

    def test_permissions_not_refreshed_yet_pauses_changes(self):
        """No helper answered (this host still has the older rules): nothing
        changes, and the message gives the command that fixes it."""
        self.live(LEGACY)
        self.sudo.unavailable = True
        r = ntfy_installer.update_config(upstream_relay=False)
        self.assertEqual((r["ok"], r["code"]), (False, "helper_unavailable"))
        self.assertIn("recover-sudoers.sh", r["command"])
        self.assertEqual(r["message"], "Notification-server changes are paused until "
                                       "Aerodrome's system permissions are refreshed — run "
                                       f"`{r['command']}` on the server.")
        self.assertEqual(self.cfg.read_text(), LEGACY)
        self.assertNotIn("written", r)

    def test_ntfy_not_coming_back_puts_the_old_files_back(self):
        """The helper's health check fails after the restart: it restores the
        previous unit and server.yml and says so; nothing is left half-done."""
        live = LEGACY + 'auth-file: "/srv/example/user.db"\n'
        self.live(live)
        before_unit = self.unit.read_text()
        self.host.healthy = False
        r = ntfy_installer.update_config(upstream_relay=False, drop_custom_keys=["auth-file"])
        self.assertFalse(r["ok"])
        self.assertIn("previous settings were put back", r["message"])
        self.assertNotIn("written", r)
        self.assertEqual(self.cfg.read_text(), live)
        self.assertEqual(self.unit.read_text(), before_unit)

    def test_a_busy_helper(self):
        self.live(LEGACY)
        fd = os.open(str(self.host.f("/run/aerodrome-helper.lock")), os.O_RDWR | os.O_CREAT, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            r = ntfy_installer.update_config(upstream_relay=False)
        finally:
            os.close(fd)
        self.assertEqual((r["ok"], r["code"], r["busy"]), (False, "busy", True))
        self.assertEqual(self.cfg.read_text(), LEGACY)


class ReadersFollowYaml(_NtfyCase):

    def test_readers_use_the_classifier(self):
        self.live("base-url: 'https://ntfy.example.net'  # c\nlisten-http: '127.0.0.1:80'  # c\n")
        self.assertEqual(ntfy_installer._read_server_port(), 80)
        self.assertEqual(ntfy_installer._read_server_bind(), "127.0.0.1")
        self.assertEqual(ntfy_installer._read_base_url(), "https://ntfy.example.net")
        self.assertFalse(ntfy_installer._read_upstream_relay())

    def test_invalid_stored_address_reads_as_none(self):
        self.live(LEGACY.replace("http://192.0.2.10:2586", "http://x?q=1"))
        self.assertIsNone(ntfy_installer._read_base_url())

    def test_missing_file(self):
        """Guard (same on v3.4.144): the readers' return contracts."""
        self.assertIsNone(ntfy_installer._read_server_port())
        self.assertTrue(ntfy_installer._read_upstream_relay())


class InstallValidatesFirst(_NtfyCase):

    STATE = "not_installed"

    def test_bad_values_return_before_any_command_or_download(self):
        for kw in ({"base_url": INJECTION}, {"base_url": "http://x y"},
                   {"port": 0}, {"bind": "192.0.2.1"}):
            r = ntfy_installer.install(**kw)
            self.assertFalse(r["ok"], kw)
            self.assertIn("message", r)
            self.assertEqual(self.sudo.calls, [], kw)

    def test_fresh_install_falls_back_to_localhost_only_without_an_address(self):
        """Pins the fresh-install localhost fallback (LAN detection failed)."""
        with mock.patch.object(ntfy_installer, "_detect_lan_ip",
                               return_value=(None, "no route")):
            r = ntfy_installer.install()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.helper_calls(), [["ntfy-install", "latest", "--port", "2586",
                                                "--bind", "0.0.0.0", "--base-url",
                                                "http://localhost:2586", "--upstream", "1"]])
        self.assertEqual(yaml.safe_load(self.cfg.read_text())["base-url"], "http://localhost:2586")
        self.assertEqual(self.unit.read_text(), ntfy_installer.NTFY_UNIT_TEXT)

    def test_a_detected_address_and_the_settings_reach_the_helper(self):
        with mock.patch.object(ntfy_installer, "_detect_lan_ip",
                               return_value=("192.0.2.44", None)):
            r = ntfy_installer.install(port=80, bind="127.0.0.1", upstream_relay=False)
        self.assertEqual((r["ok"], r["version"], r["port"], r["bind"]),
                         (True, "2.11.0", 80, "127.0.0.1"), r)
        self.assertTrue(r["topic"].startswith("aerodrome-"))
        c = ntfy_installer.classify_server_yml(self.cfg.read_text())
        self.assertEqual((c["base_url"], c["port"], c["bind"], c["upstream"]),
                         ("http://192.0.2.44:80", 80, "127.0.0.1", False))


# ---------------------------------------------------------------------------
# ntfy's message cache: the stale-data note and "Also delete cached messages"
# see both caches Aerodrome's ntfy has used — the sandboxed unit's (only root
# can see it: the helper answers) and the older root-run unit's.
# ---------------------------------------------------------------------------

_REAL_INSTALL_STATUS = ntfy_installer.install_status
HAND_CACHE_TEXT = "The cache file you set by hand in server.yml (cache-file) was left in place."
STATE_CACHE = "/var/lib/private/aerodrome-ntfy/cache.db"
LEGACY_CACHE = "/var/lib/ntfy/cache.db"


class _CacheCase(_NtfyCase):
    """Nothing installed; helpers to plant files and to use the real
    install_status() over the fake root."""

    STATE = "not_installed"

    def plant(self, *paths):
        for p in paths:
            f = self.host.f(p)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("message bodies\n")
            yield f

    def real_status(self):
        """install_status() for real over the fake root (no systemctl)."""
        for name, value in (("install_status", _REAL_INSTALL_STATUS),
                            ("_systemd_service_status", lambda *a: {"active": False,
                                                                    "enabled": False}),
                            ("_binary_version", lambda *a: None),
                            ("_detect_arch", lambda: "linux_amd64")):
            p = mock.patch.object(ntfy_installer, name, value)
            p.start()
            self.addCleanup(p.stop)


class MessageCache(_CacheCase):

    # -- the reinstall note (/api/ntfy/status stale_data) -----------------------
    def test_stale_note_sees_either_cache_and_its_wal_or_shm(self):
        for cache in (STATE_CACHE, LEGACY_CACHE):
            for side in ("", "-wal", "-shm"):
                with self.subTest(path=cache + side):
                    (f,) = self.plant(cache + side)
                    self.assertTrue(ntfy_installer.stale_data_present())
                    f.unlink()
        self.assertFalse(ntfy_installer.stale_data_present())
        self.assertEqual({tuple(c) for c in self.helper_calls()}, {("version",)})

    def test_stale_note_ignores_the_official_packages_cache(self):
        list(self.plant("/var/cache/ntfy/cache.db"))
        self.assertFalse(ntfy_installer.stale_data_present())

    def test_stale_note_is_off_while_installed_or_without_the_helper(self):
        list(self.plant(LEGACY_CACHE))
        self.sudo.unavailable = True
        self.assertFalse(ntfy_installer.stale_data_present())
        self.sudo.unavailable = False
        self.STATE = "aerodrome_managed"
        self.assertFalse(ntfy_installer.stale_data_present())

    # -- "Also delete cached messages" ------------------------------------------
    def managed_install(self):
        self.host.legacy_host()
        self.real_status()
        self.assertEqual(ntfy_installer.install_status()["state"], "aerodrome_managed")

    def test_uninstall_purge_removes_both_caches_and_nothing_of_the_packages(self):
        self.managed_install()
        planted = list(self.plant(STATE_CACHE, STATE_CACHE + "-wal", LEGACY_CACHE,
                                  LEGACY_CACHE + "-shm"))
        vendor = list(self.plant("/var/cache/ntfy/cache.db", "/var/cache/ntfy/attachments/a1"))
        r = ntfy_installer.uninstall(purge_data=True)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["purged_data"], r)
        self.assertEqual(self.helper_calls(), [["ntfy-uninstall", "--purge"]])
        for f in planted:
            self.assertFalse(f.exists(), f)
        for f in vendor:
            self.assertTrue(f.exists(), f)
        self.assertEqual(r["message"], "Uninstalled ntfy. Its cached messages were deleted too.")
        self.assertEqual(r["status"]["state"], "not_installed")

    def test_without_the_box_the_messages_stay(self):
        self.managed_install()
        planted = list(self.plant(STATE_CACHE, LEGACY_CACHE))
        r = ntfy_installer.uninstall(purge_data=False)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["purged_data"], r)
        self.assertEqual(self.helper_calls(), [["ntfy-uninstall"]])
        self.assertTrue(all(f.exists() for f in planted))
        # (no "a reinstall picks them up": one cached on the older setup,
        # in /var/lib/ntfy, stays there — a reinstall's unit uses its own)
        self.assertEqual(r["message"], "Uninstalled ntfy. Its cached messages were kept.")

    def test_a_cache_file_set_by_hand_is_left_and_the_result_says_so(self):
        """A cache-file set by hand in server.yml (a setting Aerodrome doesn't
        manage) is not Aerodrome's to delete: the result says the hand-set file
        was left. Names only — the hand-set path is never echoed."""
        self.managed_install()
        (hand,) = self.plant("/srv/example/ntfy.db")
        self.live(LEGACY.replace("cache-file: /var/lib/ntfy/cache.db", f'cache-file: "{hand}"'))
        r = ntfy_installer.uninstall(purge_data=True)
        self.assertTrue(r["ok"], r)
        self.assertTrue(hand.exists())
        self.assertIn(HAND_CACHE_TEXT, r["message"])
        self.assertNotIn(str(hand), json.dumps(r))

    def test_a_managed_cache_file_adds_no_note(self):
        self.managed_install()
        self.live(LEGACY)
        r = ntfy_installer.uninstall(purge_data=True)
        self.assertTrue(r["ok"], r)
        self.assertNotIn(HAND_CACHE_TEXT, r["message"])

    def test_without_the_helper_nothing_changes_and_the_command_is_given(self):
        self.managed_install()
        planted = list(self.plant(LEGACY_CACHE))
        self.sudo.unavailable = True
        r = ntfy_installer.uninstall(purge_data=True)
        self.assertEqual((r["ok"], r["code"], r["purged_data"]), (False, "helper_unavailable", False))
        self.assertIn("recover-sudoers.sh", r["message"])
        self.assertTrue(self.stamp.exists())
        self.assertTrue(planted[0].exists())
        self.assertEqual(r["status"]["state"], "aerodrome_managed")

    def test_leftover_purge_with_nothing_installed(self):
        planted = list(self.plant(STATE_CACHE, LEGACY_CACHE))
        self.real_status()
        r = ntfy_installer.uninstall(purge_data=True)
        self.assertEqual((r["ok"], r["purged_data"]), (True, True), r)
        self.assertEqual(r["message"], "Nothing to uninstall. Cached messages from an earlier "
                                       "install were deleted.")
        self.assertFalse(any(f.exists() for f in planted))
        self.assertFalse(ntfy_installer.stale_data_present())
        r = ntfy_installer.uninstall(purge_data=True)
        self.assertEqual(r["message"], "Nothing to uninstall.")
        self.sudo.calls.clear()
        self.assertEqual(ntfy_installer.uninstall(purge_data=False)["message"], "Nothing to uninstall.")
        self.assertEqual(self.sudo.calls, [])

    def test_the_unit_and_the_purge_name_the_same_caches(self):
        """The new unit's --cache-file sits in its StateDirectory (under
        /var/lib/private: DynamicUser); the shared block's NTFY_CACHE_FILES
        are the two cache paths a server.yml may name (the older unit's and
        the new one's); the helper purges both folders. The release gate
        (check_installers.py ntfy-cache) reads the same constants."""
        unit = ntfy_installer.NTFY_UNIT_TEXT
        self.assertIn("--cache-file /var/lib/aerodrome-ntfy/cache.db", unit)
        self.assertIn("\nStateDirectory=aerodrome-ntfy\n", unit)
        self.assertEqual(ntfy_installer.NTFY_CACHE_FILES,
                         (LEGACY_CACHE, "/var/lib/aerodrome-ntfy/cache.db"))
        self.assertEqual(self.host.h.NTFY_PURGE_DIRS,
                         ("/var/lib/private/aerodrome-ntfy", "/var/lib/ntfy"))
        self.assertNotIn("SERVER_CACHE_FILE", (REPO / "ntfy_installer.py").read_text())


class InstallStatus(_CacheCase):
    """install_status() on its own: the same answer as the helper's
    _ntfy_state() (which counts a stamped install with files missing as
    ours: 'partial' here), the older unit told apart without sudo, and an
    official package's folder it can't search read as absent."""

    def setUp(self):
        super().setUp()
        self.real_status()

    def state(self):
        return ntfy_installer.install_status()

    def test_layouts_agree_with_the_helper(self):
        h = self.host.h
        layouts = {
            "none": [],
            "ours": ["/var/lib/ntfy/aerodrome-installed", "/usr/local/bin/ntfy",
                     "/etc/ntfy/server.yml", "/etc/systemd/system/ntfy.service"],
            "ours, partial": ["/var/lib/ntfy/aerodrome-installed", "/etc/ntfy/server.yml"],
            "official package": ["/usr/bin/ntfy", "/lib/systemd/system/ntfy.service",
                                 "/etc/ntfy/server.yml"],
            "a vendor unit only": ["/usr/lib/systemd/system/ntfy.service"],
            "a server.yml only": ["/etc/ntfy/server.yml"],
        }
        want = {"none": "not_installed", "ours": "aerodrome_managed",
                "ours, partial": "partial", "official package": "external",
                "a vendor unit only": "external", "a server.yml only": "external"}
        for name, files in layouts.items():
            with self.subTest(name):
                for p in self.host.root.rglob("*"):
                    if p.is_file():
                        p.unlink()
                list(self.plant(*files))
                app = self.state()["state"]
                self.assertEqual(app, want[name])
                self.assertEqual({"partial": "aerodrome_managed"}.get(app, app), h._ntfy_state())

    def test_legacy_unit_without_sudo(self):
        self.host.legacy_host()
        self.assertIs(self.state()["legacy_unit"], True)
        self.unit.write_text(ntfy_installer.NTFY_UNIT_TEXT)
        self.assertIs(self.state()["legacy_unit"], False)
        self.unit.unlink()
        self.assertIsNone(self.state()["legacy_unit"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root searches any folder")
    def test_an_unsearchable_folder_reads_as_absent(self):
        """An official package's 0700 /var/lib/ntfy: install_status used to
        raise PermissionError."""
        list(self.plant("/usr/bin/ntfy", "/var/lib/ntfy/user.db"))
        d = self.host.f("/var/lib/ntfy")
        d.chmod(0)
        self.addCleanup(d.chmod, 0o755)
        s = self.state()
        self.assertEqual(s["state"], "external")
        self.assertFalse(s["stamp"]["present"])


# ---------------------------------------------------------------------------
# The Notifications tab (templates/config.html): every caller of
# /api/ntfy/config asks before dropping hand-added settings. The static
# checks below pin the source; ConfigHtmlSaveBehaviour runs the same
# functions under node against a stub page and a stub endpoint.
# ---------------------------------------------------------------------------

CONFIRM_TEXT = ("Your notification server's config file has settings Aerodrome doesn't "
                "manage: {names}. Saving removes them. Save anyway?")
STORED_URL_TEXT = "Your saved notification server address is no longer accepted — re-enter it."
CANCEL_TEXT = "Not saved — nothing was changed."


def _js_function(src, name):
    """The text of `async function name(` / `function name(` up to the next
    top-level function declaration."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", src, re.M)
    if not m:
        raise AssertionError(f"{name} not found")
    nxt = re.search(r"^(?:async )?function \w+\(", src[m.end():], re.M)
    return src[m.start(): m.end() + (nxt.start() if nxt else len(src))]


def _js_concat(expr, names):
    """Evaluate a `'lit' + "lit" + names.join(', ')` expression: only string
    literals without escapes and that one join, so the check fails loudly if
    the confirm text is built any other way."""
    out, pos = [], 0
    token = re.compile(r"\s*(?:'([^'\\]*)'|\"([^\"\\]*)\"|(names\.join\(', '\)))\s*(\+|$)")
    while pos < len(expr):
        m = token.match(expr, pos)
        if not m:
            raise AssertionError(f"unexpected confirm text expression at: {expr[pos:pos + 40]!r}")
        out.append(", ".join(names) if m.group(3) else (m.group(1) if m.group(1) is not None
                                                        else m.group(2)))
        pos = m.end()
    return "".join(out)


class ConfigHtmlAsksBeforeDroppingSettings(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "templates" / "config.html").read_text()

    def test_one_post_site_and_it_handles_custom_keys(self):
        self.assertEqual(self.src.count("fetch('/api/ntfy/config'"), 1)
        helper = _js_function(self.src, "nfPostNtfyConfig")
        self.assertIn("fetch('/api/ntfy/config'", helper)
        # asks again if the server lists a setting added since the last ask
        self.assertIn("while (r.status === 409 && j.code === 'custom_keys')", helper)
        # the resend names exactly what the user was shown, never a bare true
        self.assertIn("drop_custom_keys: names", helper)
        self.assertNotIn("drop_custom_keys: true", self.src)
        self.assertIn("cancelled: true", helper)

    def test_the_confirm_text_is_the_approved_wording(self):
        helper = _js_function(self.src, "nfPostNtfyConfig")
        m = re.search(r"if \(!confirm\((.*?)\)\) \{", helper, re.S)
        self.assertIsNotNone(m, "no confirm( … ) in nfPostNtfyConfig")
        for names in (["auth-file"], ["auth-default-access", "auth-file"]):
            self.assertEqual(_js_concat(m.group(1), names),
                             CONFIRM_TEXT.format(names=", ".join(names)))

    def test_every_caller_goes_through_the_helper_and_handles_cancel(self):
        for name in ("nfSaveNtfyConfig", "nfFixLocalhostBaseUrl"):
            body = _js_function(self.src, name)
            self.assertIn("await nfPostNtfyConfig(", body, name)
            self.assertIn("if (cancelled)", body, name)
            self.assertIn(CANCEL_TEXT, body, name)
        save = _js_function(self.src, "nfSaveNtfyConfig")
        self.assertIn("j.code === 'stored_value_invalid'", save)
        # from this page that can only be listen-http: the result line, not
        # the Base URL field
        self.assertIn("nfShowConfigError(resultEl, j.message || j.error)", save)
        # the relay checkbox tells the save which control fired it, so a
        # toggle that didn't save (Cancel, any refusal, a network error)
        # puts the checkbox back
        self.assertIn("onchange=\"nfSaveNtfyConfig('relay')\"", self.src)
        self.assertIn("upEl.checked = !body.upstream_relay", save)
        # empty field, Cancel, stored_value_invalid, any other refusal, network
        self.assertEqual(save.count("putRelayBack();"), 5)
        empty = re.search(r"if \(!body\.base_url\) \{(.*?)return;", save, re.S)
        self.assertIsNotNone(empty, "no empty-field branch in nfSaveNtfyConfig")
        self.assertIn("putRelayBack();", empty.group(1))
        self.assertLess(empty.group(1).index("putRelayBack();"),
                        empty.group(1).index("alertModal('Base URL required'"))
        # server.yml was written but ntfy didn't restart: the file holds the
        # new value, so re-read it instead of putting the checkbox back
        self.assertIn("if (j.written) loadNtfyStatus(); else putRelayBack();", save)

    def test_one_save_at_a_time(self):
        """A relay click or a self-heal while a save is in flight would send a
        second full POST; the first one's put-back could then contradict the
        server."""
        self.assertIsNotNone(re.search(r"(?m)^let nfConfigSaving = false;", self.src),
                             "no top-level `let nfConfigSaving = false;`")
        save = _js_function(self.src, "nfSaveNtfyConfig")
        self.assertIn("if (nfConfigSaving) {", save)
        self.assertIn("upEl.checked = !upEl.checked", save)
        heal = _js_function(self.src, "nfFixLocalhostBaseUrl")
        self.assertIn("if (nfConfigSaving) return false;", heal)
        for name, body in (("nfSaveNtfyConfig", save), ("nfFixLocalhostBaseUrl", heal)):
            self.assertIn("nfConfigSaving = true;", body, name)
            self.assertIn("finally {", body, name)
            self.assertIn("nfConfigSaving = false;", body[body.rindex("finally {"):], name)

    def test_the_resend_keeps_the_users_change(self):
        """OK resends the same body (the typed address, the relay as clicked)
        plus the names shown — not the names alone."""
        helper = _js_function(self.src, "nfPostNtfyConfig")
        self.assertIn("post(Object.assign({}, body, {drop_custom_keys: names}))", helper)

    def test_self_heal_cancel_gives_the_button_back(self):
        heal = _js_function(self.src, "nfFixLocalhostBaseUrl")
        m = re.search(r"if \(cancelled\) \{(.*?)return false;", heal, re.S)
        self.assertIsNotNone(m, "no cancelled branch in nfFixLocalhostBaseUrl")
        self.assertIn("if (btn) btn.disabled = false;", m.group(1))

    def test_inline_message_sits_under_the_base_url_field(self):
        self.assertIn(f"const NF_STORED_URL_MSG = '{STORED_URL_TEXT}';", self.src)
        self.assertIn('<div id="nfBaseUrlError"', self.src)
        self.assertIn("(cfg.problems || []).includes('base-url')", self.src)
        # only when the server reports the saved address, never always
        self.assertIn('${baseUrlProblem ? `<div id="nfBaseUrlError"', self.src)
        show = _js_function(self.src, "nfShowConfigError")
        self.assertIn("span.textContent = '⚠ ' + msg", show)     # server text as text
        self.assertNotIn("innerHTML", show)



def _node18():
    """A node >= 18 on PATH (the release gate's JavaScript parse needs one too)."""
    exe = shutil.which("node") or shutil.which("nodejs")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-p", "process.versions.node.split('.')[0]"],
                           capture_output=True, text=True, timeout=30)
        return exe if int(r.stdout.strip()) >= 18 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _js_block(src, name):
    """`async function name(` / `function name(` through its closing `}` at
    column 0 (top-level functions in config.html end that way)."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", src, re.M)
    if not m:
        raise AssertionError(f"{name} not found")
    return src[m.start(): src.index("\n}\n", m.start()) + 2]


# Runs the page's save functions with a stub DOM, confirm(), alertModal() and
# /api/ntfy/config. The stub endpoint keeps the server's relay value and its
# hand-added keys the way update_config does: 409 custom_keys until the resend
# names them all, then it writes. loadNtfyStatus() re-reads the relay from it.
# A held fetch waits until the scenario releases it, to overlap two saves.
PAGE_HARNESS = r"""
const fs = require('fs');
const {code} = JSON.parse(fs.readFileSync(0, 'utf8'));
const URL = 'http://192.0.2.10:2586';

function page(opts) {
  const S = {relay: opts.relay, custom: (opts.custom || []).slice(), posts: [], modals: [],
             confirms: [], loads: 0, held: []};
  const el = () => ({textContent: '', innerHTML: '', style: {}, disabled: false,
                     appendChild(c) { this.textContent += c.textContent; }});
  const els = {nfBaseUrl: {value: opts.url}, nfUpstreamRelay: {checked: opts.relay},
               nfConfigResult: el(), nfSaveConfigBtn: el()};
  const answers = (opts.confirms || []).slice();
  let toHold = opts.hold || 0;
  const respond = opts.respond || ((b) => {
    const left = S.custom.filter(k => !(b.drop_custom_keys || []).includes(k));
    if (left.length) return [409, {ok: false, code: 'custom_keys', custom_keys: S.custom.slice().sort()}];
    S.custom = [];
    if ('upstream_relay' in b) S.relay = b.upstream_relay;
    return [200, {ok: true}];
  });
  const env = {
    document: {getElementById: id => els[id] || null, createElement: el},
    alertModal: async (title) => { S.modals.push(title); },
    confirm: (text) => { S.confirms.push(text); return answers.length ? answers.shift() : false; },
    fetch: async (url, init) => {
      const b = JSON.parse(init.body);
      S.posts.push(b);
      if (toHold > 0) { toHold--; await new Promise(r => S.held.push(r)); }
      const [status, body] = respond(b, S);
      return {status, json: async () => body};
    },
    loadNtfyStatus: async () => { S.loads++; els.nfUpstreamRelay.checked = S.relay; },
    setTimeout: () => 0,
  };
  const names = Object.keys(env);
  const api = new Function(...names, code + '\nreturn {nfSaveNtfyConfig, nfFixLocalhostBaseUrl};')
      (...names.map(n => env[n]));
  // a click flips the box before onchange runs, as the browser does
  const click = () => { els.nfUpstreamRelay.checked = !els.nfUpstreamRelay.checked;
                        return api.nfSaveNtfyConfig('relay'); };
  const hooks = () => ({btn: {disabled: false}, status: {textContent: '', style: {}}});
  const out = (extra) => Object.assign({box: els.nfUpstreamRelay.checked, server: S.relay,
      posts: S.posts.slice(), modals: S.modals.slice(), confirms: S.confirms.slice(), loads: S.loads,
      result: els.nfConfigResult.textContent, saveBtn: els.nfSaveConfigBtn.disabled}, extra || {});
  return {S, els, api, click, hooks, out};
}

const scenarios = {
  async empty_field_relay() {
    const p = page({url: '', relay: true});
    await p.click();
    const first = p.out();
    p.els.nfBaseUrl.value = URL;           // re-entered, as note (a) asks: saves work again
    await p.click();
    return Object.assign(first, {after: p.out()});
  },
  async blank_field_relay() {
    const p = page({url: '   ', relay: false});
    await p.click();
    return p.out();
  },
  async written_but_restart_failed() {
    const p = page({url: URL, relay: true, respond: (b, S) => {
      S.relay = b.upstream_relay;
      return [400, {ok: false, written: true, dropped_keys: [],
                    message: 'Config written but failed to restart: example'}];
    }});
    await p.click();
    return p.out();
  },
  async refused_nothing_written() {
    const p = page({url: URL, relay: true,
                    respond: () => [400, {ok: false, message: 'Command failed: example'}]});
    await p.click();
    return p.out();
  },
  async network_error_then_retry() {
    let calls = 0;
    const p = page({url: URL, relay: true, respond: (b, S) => {
      if (calls++ === 0) throw new Error('offline');
      S.relay = b.upstream_relay;
      return [200, {ok: true}];
    }});
    await p.click();
    const first = p.out();
    await p.click();
    return Object.assign(first, {after: p.out()});
  },
  async double_toggle_both_cancelled() {
    const p = page({url: URL, relay: true, custom: ['auth-file'], hold: 2, confirms: [false, false]});
    const a = p.click();
    const b = p.click();
    p.S.held.shift()();
    await a;
    if (p.S.held.length) p.S.held.shift()();
    await b;
    return p.out();
  },
  async relay_click_during_selfheal() {
    const p = page({url: URL, relay: true, hold: 1});
    const h = p.hooks();
    const heal = p.api.nfFixLocalhostBaseUrl('192.0.2.20', 2586, h);
    await p.click();
    p.S.held.shift()();
    const healed = await heal;
    return p.out({healed});
  },
  async selfheal_during_save() {
    const p = page({url: URL, relay: true, hold: 1});
    const save = p.api.nfSaveNtfyConfig();
    const h = p.hooks();
    const healed = await p.api.nfFixLocalhostBaseUrl('192.0.2.20', 2586, h);
    p.S.held.shift()();
    await save;
    return p.out({healed, healBtn: h.btn.disabled});
  },
  async ok_resends_with_the_change() {
    const p = page({url: URL, relay: true, custom: ['auth-file'], confirms: [true]});
    await p.click();
    return p.out();
  },
  async cancel_puts_the_relay_back() {
    const p = page({url: URL, relay: true, custom: ['auth-file'], confirms: [false]});
    await p.click();
    return p.out();
  },
  async selfheal_cancel() {
    const p = page({url: URL, relay: true, custom: ['auth-file'], confirms: [false]});
    const h = p.hooks();
    const healed = await p.api.nfFixLocalhostBaseUrl('192.0.2.20', 2586, h);
    const first = p.out({healed, healBtn: h.btn.disabled, healStatus: h.status.textContent});
    const again = await p.api.nfFixLocalhostBaseUrl('192.0.2.20', 2586, p.hooks());
    return Object.assign(first, {posts_after_retry: p.S.posts.length, again});
  },
};

(async () => {
  const res = {};
  for (const [name, fn] of Object.entries(scenarios)) {
    try { res[name] = await fn(); } catch (e) { res[name] = {error: String(e && e.stack || e)}; }
  }
  process.stdout.write(JSON.stringify(res));
})();
"""


class ConfigHtmlSaveBehaviour(unittest.TestCase):
    """The Notifications tab's save code as shipped (nfPostNtfyConfig,
    nfShowConfigError, nfSaveNtfyConfig, nfFixLocalhostBaseUrl and the nf*
    declarations, cut from templates/config.html) run under node against a
    stub page and a stub /api/ntfy/config. The relay checkbox must end up
    showing what the server holds, whatever the outcome."""

    URL = "http://192.0.2.10:2586"

    @classmethod
    def setUpClass(cls):
        node = _node18()
        if not node:
            raise unittest.SkipTest("needs node >= 18")
        src = (REPO / "templates" / "config.html").read_text()
        decls = re.findall(r"(?m)^(?:let|const) (?:nf|NF_)\w* = [^\n]*$", src)
        code = "\n".join(decls + [_js_block(src, n) for n in (
            "nfPostNtfyConfig", "nfShowConfigError", "nfSaveNtfyConfig", "nfFixLocalhostBaseUrl",
            "nfHelperAlert")])
        r = subprocess.run([node, "-e", PAGE_HARNESS], input=json.dumps({"code": code}),
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise AssertionError(f"harness failed: {r.stderr[-2000:]}")
        cls.res = json.loads(r.stdout)

    def run_of(self, name):
        res = self.res[name]
        self.assertNotIn("error", res, res.get("error"))
        return res

    def test_relay_toggle_with_an_empty_field_is_put_back(self):
        """Note (a)'s state: the saved address was rejected, so the field is
        empty while the relay box shows the real setting."""
        for name in ("empty_field_relay", "blank_field_relay"):
            r = self.run_of(name)
            self.assertEqual(r["posts"], [], name)
            self.assertEqual(r["modals"], ["Base URL required"], name)
            self.assertEqual(r["box"], r["server"], name)
        after = self.res["empty_field_relay"]["after"]
        self.assertEqual(len(after["posts"]), 1)
        self.assertEqual(after["box"], after["server"])

    def test_written_but_not_restarted_shows_what_the_file_holds(self):
        r = self.run_of("written_but_restart_failed")
        self.assertFalse(r["server"])
        self.assertEqual(r["box"], r["server"])
        self.assertGreaterEqual(r["loads"], 1)
        self.assertEqual(r["modals"], ["Save failed"])

    def test_a_refusal_that_wrote_nothing_puts_the_box_back(self):
        r = self.run_of("refused_nothing_written")
        self.assertTrue(r["server"])
        self.assertEqual(r["box"], r["server"])
        self.assertEqual(r["loads"], 0)
        self.assertEqual(r["modals"], ["Save failed"])

    def test_a_network_error_puts_the_box_back_and_the_next_save_runs(self):
        r = self.run_of("network_error_then_retry")
        self.assertEqual(r["box"], r["server"])
        self.assertEqual(r["modals"], ["Save failed"])
        self.assertEqual(len(r["after"]["posts"]), 2)
        self.assertFalse(r["after"]["server"])
        self.assertEqual(r["after"]["box"], r["after"]["server"])
        self.assertFalse(r["after"]["saveBtn"])

    def test_two_quick_relay_clicks_send_one_save(self):
        """Both 409s cancelled used to leave the box opposite the server."""
        r = self.run_of("double_toggle_both_cancelled")
        self.assertEqual(len(r["posts"]), 1)
        self.assertTrue(r["server"])
        self.assertEqual(r["box"], r["server"])
        self.assertEqual(r["result"], CANCEL_TEXT)

    def test_no_second_save_while_the_self_heal_is_saving(self):
        r = self.run_of("relay_click_during_selfheal")
        self.assertEqual(r["posts"], [{"base_url": "http://192.0.2.20:2586"}])
        self.assertTrue(r["healed"])
        self.assertEqual(r["box"], r["server"])
        r = self.run_of("selfheal_during_save")
        self.assertEqual(len(r["posts"]), 1)
        self.assertFalse(r["healed"])
        self.assertFalse(r["healBtn"])

    def test_ok_resends_the_users_change_with_the_names_shown(self):
        r = self.run_of("ok_resends_with_the_change")
        self.assertEqual(r["confirms"], [CONFIRM_TEXT.format(names="auth-file")])
        self.assertEqual(r["posts"][1], {"base_url": self.URL, "upstream_relay": False,
                                         "drop_custom_keys": ["auth-file"]})
        self.assertFalse(r["server"])
        self.assertEqual(r["box"], r["server"])

    def test_cancel_puts_the_relay_back_and_says_so(self):
        r = self.run_of("cancel_puts_the_relay_back")
        self.assertEqual(len(r["posts"]), 1)
        self.assertEqual(r["box"], r["server"])
        self.assertEqual(r["result"], CANCEL_TEXT)
        self.assertFalse(r["saveBtn"])

    def test_self_heal_cancel_gives_the_button_back(self):
        r = self.run_of("selfheal_cancel")
        self.assertFalse(r["healed"])
        self.assertFalse(r["healBtn"])
        self.assertEqual(r["healStatus"], CANCEL_TEXT)
        self.assertEqual(r["posts_after_retry"], 2)     # not stuck after a cancel


# ---------------------------------------------------------------------------
# templates/config.html: the ntfy panel's leftover-messages note and
# uninstall prompt, and the page's message box (alertModal) — the approved
# wording and styling
# ---------------------------------------------------------------------------

STALE_NOTE_TEXT = ("Cached messages from a prior install are still on this server. "
                   "To start fresh, run this on the Aerodrome machine first, then reinstall:")
PURGE_HINT_TEXT = ("Deletes the notification server's cached messages. Leave unchecked if you "
                   "might reinstall later and want to keep your message history.")
STALE_CMD_RE = re.compile(r'<code id="nfStaleCacheCmd"[^>]*>([^<]*)</code>')
COPY_ONCLICK = ("onclick=\"copyToClipboard(document.getElementById('nfStaleCacheCmd')"
                ".textContent, this)\">Copy</button>")


def _squash(s):
    return re.sub(r"\s+", " ", s).strip()


def _stale_cmd():
    """The command the page gives for leftover messages (v3.4.150): the
    system helper's purge with nothing installed — both caches Aerodrome's
    ntfy has used, one of them in a folder only root can see."""
    return f"sudo {privhelper.HELPER} ntfy-uninstall --purge"


def _css_rules(src):
    """{selector: {property: value}} of the top-level rules in the page's own
    <style> block (comments dropped; a selector seen twice merges). Rules
    inside an @media/@supports/@keyframes block are left out: they don't
    apply on every screen."""
    style = src[src.index("<style>") + len("<style>"): src.index("</style>")]
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
    rules, depth, head, sel, start = {}, 0, 0, "", 0
    for i, ch in enumerate(style):
        if ch == "{":
            if depth == 0:
                sel, start = style[head:i].rsplit(";", 1)[-1], i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise AssertionError("unbalanced } in config.html's <style>")
            if depth == 0:
                head = i + 1
                if not sel.strip().startswith("@"):
                    decls = rules.setdefault(_squash(sel), {})
                    for d in style[start:i].split(";"):
                        if ":" in d:
                            k, v = d.split(":", 1)
                            decls[k.strip()] = v.strip()
    if depth:
        raise AssertionError("unbalanced { in config.html's <style>")
    return rules


class ConfigHtmlNtfyPanelWording(unittest.TestCase):
    """The "Not installed" panel offers only Install, so its leftover-messages
    note gives the command that deletes them (with a Copy button) instead of
    sending the user to an uninstall the panel doesn't offer; the uninstall
    prompt no longer promises to delete attachments."""

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "templates" / "config.html").read_text()
        cls.panel = _js_function(cls.src, "_renderNtfyPanel")
        m = re.search(r"const staleNote = statusPayload\.stale_data\s*\?\s*`(.*?)`\s*:\s*'';",
                      cls.panel, re.S)
        if not m:
            raise AssertionError("no staleNote in _renderNtfyPanel")
        cls.note = m.group(1)

    def test_the_note_is_the_approved_wording(self):
        self.assertIn(STALE_NOTE_TEXT, _squash(self.note))
        self.assertNotIn("run uninstall with", _squash(self.src))

    def test_the_command_is_the_helpers_purge(self):
        """No wildcard (it would expand as the user, who can't list the
        folders) and no paths: the helper knows both caches."""
        self.assertEqual(STALE_CMD_RE.findall(self.note), [_stale_cmd()])
        self.assertEqual(self.src.count('id="nfStaleCacheCmd"'), 1)
        self.assertNotIn("*", STALE_CMD_RE.search(self.note).group(1))

    def test_no_single_cache_path_is_named(self):
        """There are two (v3.4.150): the note and the uninstall prompt name
        neither."""
        prompt = _js_function(self.src, "nfUninstallPrompt")
        for where in (self.note, prompt):
            self.assertNotIn("/var/lib/ntfy/cache.db", where)
            self.assertNotIn("/var/lib/aerodrome-ntfy", where)

    def test_the_copy_button_uses_the_pages_clipboard_helper(self):
        """copyToClipboard(text, feedbackEl) falls back to execCommand('copy'),
        so the button works over plain HTTP too."""
        self.assertIn(COPY_ONCLICK, self.note)
        self.assertIn('<button type="button" class="btn"', self.note)
        helper = _js_function(self.src, "copyToClipboard")
        self.assertIn("document.execCommand('copy')", helper)

    def test_the_note_keeps_its_amber_style(self):
        """Guard (same on v3.4.145)."""
        self.assertIn("background:rgba(245,158,11,.08)", self.note)
        self.assertIn("border-left:2px solid var(--amber)", self.note)

    def _styles(self):
        m = re.search(r'<div style="([^"]*)">\s*<code id="nfStaleCacheCmd" style="([^"]*)">'
                      r'[^<]*</code>\s*<button type="button" class="btn" style="([^"]*)"',
                      self.note)
        self.assertIsNotNone(m, "no command row (div > code#nfStaleCacheCmd + button.btn)")
        return [dict((k.strip(), v.strip()) for k, v in
                     (d.split(":", 1) for d in _squash(s).split(";") if ":" in d))
                for s in m.groups()]

    def test_the_copy_button_has_the_approved_fill(self):
        """The approved mockup fills the button bg3 on the bg2 row; the
        shared .btn is bg2 itself, so it vanished into the row."""
        row, _code, button = self._styles()
        self.assertEqual(row.get("background"), "var(--bg2)")
        self.assertEqual(button.get("background"), "var(--bg3)")

    def test_on_a_narrow_screen_copy_drops_under_the_command(self):
        """Guard (as committed in the approved-changes commit): on a phone
        the row wraps, so Copy goes under the command and no path splits
        mid-name; on a wide screen it is one line with Copy on the right."""
        row, code, button = self._styles()
        self.assertEqual(row.get("display"), "flex")
        self.assertEqual(row.get("flex-wrap"), "wrap")
        self.assertEqual(code.get("flex"), "1 1 260px")
        self.assertEqual(code.get("min-width"), "0")
        self.assertEqual(code.get("overflow-wrap"), "anywhere")
        self.assertEqual(button.get("flex"), "none")
        self.assertEqual(button.get("margin-left"), "auto")

    def test_the_uninstall_prompt_no_longer_promises_attachments(self):
        prompt = _js_function(self.src, "nfUninstallPrompt")
        self.assertIn(PURGE_HINT_TEXT, _squash(prompt))
        self.assertNotIn("attachment", prompt.lower())


# The panel as rendered: _renderNtfyPanel() cut from config.html, called with
# an install-less status (with and without leftover messages); then the Copy
# button's own onclick, run with `this` = the button.
PANEL_HARNESS = r"""
const fs = require('fs');
const {code} = JSON.parse(fs.readFileSync(0, 'utf8'));
const render = new Function('escapeAttr', code + '\nreturn _renderNtfyPanel;')(s => String(s));
const status = {state: 'not_installed', installable: true};
const out = {stale: render({status, stale_data: true}), fresh: render({status, stale_data: false})};
const cmd = out.stale.match(/<code id="nfStaleCacheCmd"[^>]*>([^<]*)<\/code>/);
const click = out.stale.match(/<button([^>]*)onclick="([^"]*)"[^>]*>Copy<\/button>/);
if (cmd && click) {
  const btn = {id: 'copy-button'};
  const calls = [];
  const doc = {getElementById: id => (id === 'nfStaleCacheCmd' ? {textContent: cmd[1]} : null)};
  new Function('document', 'copyToClipboard', click[2])
      .call(btn, doc, (text, el) => { calls.push([text, el === btn]); });
  out.copied = calls;
}
process.stdout.write(JSON.stringify(out));
"""


class ConfigHtmlStaleNoteRenders(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        node = _node18()
        if not node:
            raise unittest.SkipTest("needs node >= 18")
        src = (REPO / "templates" / "config.html").read_text()
        r = subprocess.run([node, "-e", PANEL_HARNESS],
                           input=json.dumps({"code": _js_block(src, "_renderNtfyPanel")}),
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise AssertionError(f"harness failed: {r.stderr[-2000:]}")
        cls.res = json.loads(r.stdout)

    def test_copy_puts_the_command_on_the_clipboard(self):
        self.assertEqual(self.res.get("copied"), [[_stale_cmd(), True]])

    def test_the_rendered_note_reads_as_approved(self):
        """As text: the note, then the command, then its Copy button."""
        def text(html):
            return _squash(re.sub(r"<[^>]+>", "", html))
        self.assertIn(text(STALE_NOTE_TEXT) + " " + _stale_cmd() + " Copy",
                      text(self.res["stale"]))
        self.assertIn("Install local ntfy", self.res["stale"])

    def test_no_note_without_leftover_messages(self):
        """Guard (same on v3.4.145)."""
        self.assertNotIn("Cached messages from a prior install", self.res["fresh"])
        self.assertNotIn("nfStaleCacheCmd", self.res["fresh"])
        self.assertIn("Install local ntfy", self.res["fresh"])


# v3.4.150: _renderNtfyPanel() for a managed install still on the older,
# root-run setup (legacy_unit), and nfHelperAlert() with each helper answer.
LEGACY_HARNESS = r"""
const fs = require('fs');
const {code} = JSON.parse(fs.readFileSync(0, 'utf8'));
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/'/g, '&#39;');
const modals = [];
const fns = new Function('escapeAttr', 'alertModal', 'NF_STORED_URL_MSG',
    code + '\nreturn {render: _renderNtfyPanel, alert: nfHelperAlert};')(
    esc, (t, m, k) => { modals.push([t, m, k]); return Promise.resolve(); }, 'x');
const status = {state: 'aerodrome_managed', version: '2.11.0', legacy_unit: true,
                service: {active: true}};
const cmd = '/usr/local/libexec/aerodrome/aerodrome-helper ntfy-migrate';
const out = {};
for (const [name, config] of Object.entries({
    plain: {migrate_command: 'sudo ' + cmd, custom_keys: [], problems: []},
    custom: {migrate_command: 'sudo ' + cmd + ' --drop-custom-keys auth-file',
             custom_keys: ['auth-file'], problems: []},
    address: {migrate_command: 'sudo ' + cmd, custom_keys: [], problems: ['base-url']},
    listen: {migrate_command: 'sudo ' + cmd, custom_keys: [], problems: ['listen-http']},
})) {
    out[name] = fns.render({status, config});
}
out.current = fns.render({status: Object.assign({}, status, {legacy_unit: false}),
                          config: {custom_keys: [], problems: []}});
for (const code of ['still_running', 'busy', 'helper_unavailable', undefined]) {
    fns.alert({code, message: 'm-' + code}, 'Install failed');
}
out.modals = modals;
process.stdout.write(JSON.stringify(out));
"""


class ConfigHtmlOlderSetupNote(unittest.TestCase):
    """The managed panel says when the notification server still runs on its
    older setup, why it hasn't moved yet, and the command that moves it when
    that can work; the helper's answers that aren't failures get their own
    alert titles."""

    @classmethod
    def setUpClass(cls):
        node = _node18()
        if not node:
            raise unittest.SkipTest("needs node >= 18")
        src = (REPO / "templates" / "config.html").read_text()
        code = "\n".join(_js_block(src, n) for n in
                         ("_nfLegacyNote", "_renderNtfyPanel", "nfHelperAlert"))
        r = subprocess.run([node, "-e", LEGACY_HARNESS], input=json.dumps({"code": code}),
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise AssertionError(f"harness failed: {r.stderr[-2000:]}")
        cls.res = json.loads(r.stdout)

    @staticmethod
    def text(html):
        import html as _html
        return _squash(_html.unescape(re.sub(r"<[^>]+>", " ", html)))

    def note(self, name):
        m = re.search(r'<div id="nfLegacyNote".*?</div>\s*</div>|<div id="nfLegacyNote".*?</div>',
                      self.res[name], re.S)
        return self.text(m.group(0)) if m else None

    def test_each_reason_reads_as_written(self):
        self.assertIn("The notification server still runs on its older setup. Saving any setting "
                      "in this tab moves it to its restricted setup; or run this on the server: "
                      "sudo /usr/local/libexec/aerodrome/aerodrome-helper ntfy-migrate Copy",
                      self.note("plain"))
        self.assertIn("because its config has settings Aerodrome doesn't manage (auth-file). Saving "
                      "from this tab asks before removing them; or run this on the server: "
                      "sudo /usr/local/libexec/aerodrome/aerodrome-helper ntfy-migrate "
                      "--drop-custom-keys auth-file Copy", self.note("custom"))
        self.assertIn("its saved address is no longer accepted. Saving the address below "
                      "finishes the move.", self.note("address"))
        self.assertNotIn("ntfy-migrate", self.note("address"))       # it would refuse
        self.assertIn("a value Aerodrome can't carry over (listen-http). Correct it in "
                      "/etc/ntfy/server.yml, then run this on the server:", self.note("listen"))

    def test_no_note_once_moved(self):
        self.assertIsNone(self.note("current"))
        self.assertNotIn("older setup", self.res["current"])

    def test_helper_answers_get_their_own_titles(self):
        self.assertEqual(self.res["modals"], [
            ["Still working", "m-still_running", "info"], ["Busy", "m-busy", "warning"],
            ["System permissions need a refresh", "m-helper_unavailable", "warning"],
            ["Install failed", "m-undefined", "error"]])

    def test_every_ntfy_action_uses_it(self):
        src = (REPO / "templates" / "config.html").read_text()
        for fn, title in (("nfInstallNtfy", "Install failed"), ("nfUpgradeNtfy", "Upgrade failed"),
                          ("nfUninstallNtfy", "Uninstall failed"), ("nfSaveNtfyConfig", "Save failed")):
            with self.subTest(fn):
                self.assertIn(f"nfHelperAlert(j, '{title}')", _js_function(src, fn))


# executeTransition() cut from switch-to-real.html and run against a stub
# page and a stub /api/setup/switch-to-real reply.
SWITCH_HARNESS = r"""
const fs = require('fs');
const {code, replies} = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const out = [];
  for (const reply of replies) {
    const els = {}, calls = [];
    const classes = () => { const s = new Set(); return {
        add: x => s.add(x), remove: x => s.delete(x), contains: x => s.has(x),
        toggle: (x, on) => (on ? s.add(x) : s.delete(x))}; };
    const value = {recvIp: '192.0.2.10', recvPort: '30005', recvPath: '', recvLat: '', recvLon: ''};
    const document = {
      getElementById: id => els[id] || (els[id] = {value: value[id] || '', textContent: '',
                                                   classList: classes()}),
      querySelectorAll: () => [],
    };
    const run = new Function('document', 'fetch', 'alert', 'goToStep', '_setProgressState',
                             '_showError', code + '\nreturn executeTransition;')(
      document, async () => ({json: async () => reply}), m => calls.push(['alert', m]),
      n => calls.push(['step', n]), (n, s) => calls.push(['state', n, s]),
      m => calls.push(['error', m]));
    await run();
    const w = document.getElementById('feederWarning'), r = document.getElementById('feederRemoved');
    out.push({calls, warning: w.textContent, warningShown: !w.classList.contains('hidden'),
              removedShown: !r.classList.contains('hidden')});
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


class SwitchToRealFinishesWithAWarning(unittest.TestCase):
    """v3.4.150: a demo feeder the system helper couldn't remove doesn't stop
    the switch; the wizard's last step says how to remove it."""

    def test_ok_with_a_failed_feeder_step_reaches_step_four(self):
        """The server's reply when the helper couldn't remove the feeder: ok,
        no error, a warning and step 1 failed — step 4, the warning shown and
        the "feeder has been removed" sentence hidden (before v3.4.150's page:
        "Transition failed at an unknown step."). A plain success and a real
        failure are unchanged."""
        node = _node18()
        if not node:
            self.skipTest("needs node >= 18")
        src = (REPO / "templates" / "switch-to-real.html").read_text()
        warning = ("The demo feeder couldn't be removed (Aerodrome's system permissions need a "
                   "refresh). It is harmless — it only listens on 127.0.0.1.")
        steps = [{"name": "stop_feeder_service", "ok": False, "message": warning},
                 {"name": "write_config", "ok": True}, {"name": "restart_aerodrome", "ok": True}]
        replies = [{"ok": True, "error": None, "warning": warning, "steps": steps},
                   {"ok": True, "error": None, "steps": [dict(s, ok=True) for s in steps]},
                   {"ok": False, "error": "The receiver didn't answer.", "steps": steps[:1]}]
        r = subprocess.run([node, "-e", SWITCH_HARNESS],
                           input=json.dumps({"code": _js_block(src, "executeTransition"),
                                             "replies": replies}),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        warned, plain, failed = json.loads(r.stdout)
        self.assertEqual(warned["calls"][-1], ["step", 4], warned["calls"])
        self.assertNotIn("error", [c[0] for c in warned["calls"]])
        self.assertIn(["state", "stop_feeder_service", "failed"], warned["calls"])
        self.assertEqual((warned["warning"], warned["warningShown"], warned["removedShown"]),
                         (warning, True, False))
        self.assertEqual(plain["calls"][-1], ["step", 4])
        self.assertEqual((plain["warningShown"], plain["removedShown"]), (False, True))
        self.assertEqual(failed["calls"][-1], ["error", "The receiver didn't answer."])
        self.assertNotIn(["step", 4], failed["calls"])

    def test_step_four_shows_the_warning(self):
        src = (REPO / "templates" / "switch-to-real.html").read_text()
        self.assertIn('<div class="warn-box hidden" id="feederWarning"></div>', src)
        self.assertIn("if (data.ok) {", src)
        self.assertIn("document.getElementById('feederWarning').textContent = warning;", src)
        self.assertNotIn("data.ok && !seenFailed", src)
        self.assertIn('<span id="feederRemoved">The synthetic feeder has been removed. </span>', src)


# copyToClipboard() cut from config.html and run against a stub DOM, a stub
# clipboard and a fake clock: over plain HTTP (no navigator.clipboard, so the
# execCommand fallback), over HTTPS (the Clipboard API), and with copying
# refused. Each scenario returns the button's label/colour at given times and
# where keyboard focus is after the copy.
COPY_HARNESS = r"""
const fs = require('fs');
const {code, text} = JSON.parse(fs.readFileSync(0, 'utf8'));

function world({secure, execOk}) {
  const W = {now: 0, timers: new Map(), seq: 0, copied: []};
  const setT = (fn, ms) => { const id = ++W.seq; W.timers.set(id, {at: W.now + ms, fn}); return id; };
  const clearT = id => { W.timers.delete(id); };
  W.advance = ms => {
    const end = W.now + ms;
    for (;;) {
      let next = null;
      for (const [id, t] of W.timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
      if (!next) break;
      W.timers.delete(next[0]); W.now = next[1].at; next[1].fn();
    }
    W.now = end;
  };
  const doc = {copied: W.copied};
  const body = {tagName: 'BODY', isConnected: true, kids: [],
    appendChild(el) { this.kids.push(el); el.isConnected = true; },
    removeChild(el) {
      this.kids.splice(this.kids.indexOf(el), 1); el.isConnected = false;
      if (doc.activeElement === el) doc.activeElement = body;
    }};
  doc.body = body;
  doc.activeElement = body;
  doc.createElement = tag => {
    const el = {tagName: tag.toUpperCase(), value: '', style: {}, isConnected: false,
                setAttribute() {}, select() {}, setSelectionRange() {},
                focus() { doc.activeElement = el; }};
    return el;
  };
  doc.execCommand = cmd => {
    if (cmd !== 'copy') return false;
    if (execOk) W.copied.push(doc.activeElement.value);
    return execOk;
  };
  const nav = secure ? {clipboard: {writeText: async t => { W.copied.push(t); }}} : {};
  W.copy = new Function('document', 'navigator', 'window', 'setTimeout', 'clearTimeout',
                        code + '\nreturn copyToClipboard;')(doc, nav, {isSecureContext: !!secure}, setT, clearT);
  W.doc = doc;
  W.button = {tagName: 'BUTTON', textContent: 'Copy', style: {color: ''}, isConnected: true,
              focus() { doc.activeElement = W.button; }};
  return W;
}
const look = W => [W.button.textContent, W.button.style.color];

async function run(opts, clicksAt, sampleAt) {
  const W = world(opts);
  const out = {samples: {}, results: []};
  const events = [...clicksAt.map(t => ['click', t]), ...sampleAt.map(t => ['sample', t])]
      .sort((a, b) => a[1] - b[1] || (a[0] === 'click' ? -1 : 1));
  for (const [kind, t] of events) {
    W.advance(t - W.now);
    if (kind === 'click') {
      W.button.focus();
      out.results.push(await W.copy(text, W.button));
      out.focusAfter = W.doc.activeElement === W.button ? 'button' : W.doc.activeElement.tagName;
      out.leftInBody = W.doc.body.kids.length;
    } else {
      out.samples[t] = look(W);
    }
  }
  out.copied = W.copied;
  return out;
}

(async () => {
  const res = {
    http_once: await run({secure: false, execOk: true}, [0], [100, 1600]),
    http_twice: await run({secure: false, execOk: true}, [0, 500], [600, 1600, 2100, 6000]),
    https_twice: await run({secure: true}, [0, 500], [600, 2100, 6000]),
    blocked: await run({secure: false, execOk: false}, [0], [100, 1600, 6000]),
  };
  process.stdout.write(JSON.stringify(res));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


class ConfigHtmlCopyButtonFeedback(unittest.TestCase):
    """The page's copyToClipboard(text, button), behind the leftover-messages
    note's Copy button (and the onboarding and CARTO key copy buttons): the
    result shows for 1.5 s and the button then reads "Copy" again — after a
    quick second click and after a refused copy too — and over plain HTTP
    keyboard focus stays on the button."""

    COPIED = ["✓ copied!", "var(--green, #22c55e)"]
    BLOCKED = ["✗ blocked", "var(--red)"]
    LABEL = ["Copy", ""]

    @classmethod
    def setUpClass(cls):
        node = _node18()
        if not node:
            raise unittest.SkipTest("needs node >= 18")
        src = (REPO / "templates" / "config.html").read_text()
        r = subprocess.run([node, "-e", COPY_HARNESS],
                           input=json.dumps({"code": _js_block(src, "copyToClipboard"),
                                             "text": _stale_cmd()}),
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise AssertionError(f"harness failed: {r.stderr[-2000:]}")
        cls.res = json.loads(r.stdout)

    def test_one_click_shows_copied_then_the_label(self):
        """Guard (same on v3.4.145)."""
        r = self.res["http_once"]
        self.assertEqual(r["results"], [True])
        self.assertEqual(r["copied"], [_stale_cmd()])
        self.assertEqual(r["samples"]["100"], self.COPIED)
        self.assertEqual(r["samples"]["1600"], self.LABEL)

    def test_a_quick_second_click_still_goes_back_to_the_label(self):
        for name in ("http_twice", "https_twice"):
            with self.subTest(name):
                r = self.res[name]
                self.assertEqual(r["copied"], [_stale_cmd()] * 2)
                self.assertEqual(r["samples"]["600"], self.COPIED)
                self.assertEqual(r["samples"]["2100"], self.LABEL)
                self.assertEqual(r["samples"]["6000"], self.LABEL)
        # the second click restarts the 1.5 s
        self.assertEqual(self.res["http_twice"]["samples"]["1600"], self.COPIED)

    def test_a_refused_copy_shows_blocked_then_the_label(self):
        r = self.res["blocked"]
        self.assertEqual(r["results"], [False])
        self.assertEqual(r["samples"]["100"], self.BLOCKED)
        self.assertEqual(r["samples"]["1600"], self.LABEL)
        self.assertEqual(r["samples"]["6000"], self.LABEL)

    def test_over_plain_http_focus_stays_on_the_button(self):
        for name in ("http_once", "blocked"):
            with self.subTest(name):
                self.assertEqual(self.res[name]["focusAfter"], "button")
                self.assertEqual(self.res[name]["leftInBody"], 0)   # the textarea is gone


class ConfigHtmlAlertModalStyled(unittest.TestCase):
    """alertModal() carries index.html's .modal-bg/.modal class names, whose
    CSS this page never loads, so every message box it showed was unstyled
    text at the foot of the page. The page's own <style> now styles it by its
    alert-modal classes: a fixed, centred, dimmed backdrop; a box in the
    page's modal look; and a long message scrolls inside the box so the title
    and OK stay on screen."""

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "templates" / "config.html").read_text()
        cls.css = _css_rules(cls.src)
        cls.fn = _js_function(cls.src, "alertModal")

    def rule(self, selector):
        self.assertIn(selector, self.css, f"no `{selector}` rule in config.html's <style>")
        return self.css[selector]

    def test_the_markup_carries_the_classes_the_css_keys_on(self):
        """Guard (same on v3.4.145): the CSS-only fix relies on these."""
        for needle in ("bg.className = 'modal-bg on alert-modal-bg';",
                       "modal.className = 'modal alert-modal';",
                       'class="alert-modal-body"', 'class="modal-title"',
                       'class="modal-actions"', 'class="modal-btn modal-btn-primary alert-modal-ok"'):
            self.assertIn(needle, self.fn)

    def test_a_fixed_centred_dimmed_backdrop(self):
        bg = self.rule(".alert-modal-bg")
        for k, v in (("position", "fixed"), ("inset", "0"), ("display", "flex"),
                     ("align-items", "center"), ("justify-content", "center"),
                     ("padding", "20px"), ("background", "rgba(0,0,0,.7)")):
            self.assertEqual(bg.get(k), v, k)

    def test_the_box_has_the_pages_modal_look(self):
        box, page_modal = self.rule(".alert-modal"), self.rule(".br-modal-inner")
        for k in ("background", "border", "border-radius"):
            self.assertEqual(box.get(k), page_modal.get(k), k)
        self.assertEqual(self.rule(".alert-modal .modal-title").get("font-weight"), "600")
        btn = self.rule(".alert-modal .modal-btn")
        self.assertEqual(btn.get("background"), "var(--cyan)")
        self.assertEqual(btn.get("color"), "var(--bg0)")
        self.assertEqual(self.rule(".alert-modal .modal-actions").get("justify-content"), "flex-end")

    def test_a_long_message_scrolls_inside_the_box(self):
        """max-height:100% is the backdrop's visible height less its padding;
        100vh is taller whenever browser toolbars take part of the screen."""
        box = self.rule(".alert-modal")
        self.assertEqual(box.get("max-height"), "100%")
        self.assertEqual(box.get("display"), "flex")
        self.assertEqual(box.get("flex-direction"), "column")
        # fills the backdrop up to alertModal()'s inline max-width rather
        # than shrinking to its text
        self.assertEqual(box.get("width"), "100%")
        body = self.rule(".alert-modal .alert-modal-body")
        self.assertEqual(body.get("overflow-y"), "auto")
        self.assertEqual(body.get("min-height"), "0")
        # a long unbroken word (a URL in an error) wraps instead of
        # scrolling sideways inside the box
        self.assertEqual(body.get("overflow-wrap"), "anywhere")

    def test_rules_inside_a_media_query_are_not_read_as_page_wide(self):
        """The checks above read only top-level rules: the box must be
        styled on every screen, not only inside an @media block."""
        self.assertEqual(
            _css_rules("<style>@media (max-width:1px) { .alert-modal-bg { position:fixed; } }"
                       "\n.example-rule { color:red; }</style>"),
            {".example-rule": {"color": "red"}})

    def test_styled_on_this_page_only(self):
        """Guard: alertModal() lives only in config.html, so its styling is
        inline here, not in the shared theme.css."""
        self.assertNotIn("alert-modal", (REPO / "static" / "theme.css").read_text())


# ---------------------------------------------------------------------------
# Endpoints and restore on a real get_app() instance
# ---------------------------------------------------------------------------

def _endpoint(app, path, method):
    return next(r.endpoint for r in app.routes
                if getattr(r, "path", "") == path and method in getattr(r, "methods", ()))


def _out(resp):
    """(status, body) for a dict or a JSONResponse."""
    if isinstance(resp, dict):
        return 200, resp
    return resp.status_code, json.loads(resp.body)


class _AppCase(_NtfyCase):

    def setUp(self):
        super().setUp()
        self.install_dir = self.tmp / "install"
        (self.install_dir / ".backups").mkdir(parents=True)
        (self.install_dir / "static").mkdir()
        db = str(self.tmp / "history.db")
        collector.init_db(db)
        conn = sqlite3.connect(db)
        apply_schema_migrations(conn, "test")
        conn.close()
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["data"]["db_file"] = db
        cfg["watchlist"] = []
        cfg_path = self.install_dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        # install_dir = Path(server.__file__).parent: point it at the temp dir;
        # no live sudoers file (the startup drift check reads nothing).
        for p in (mock.patch.object(server, "__file__", str(self.install_dir / "server.py")),
                  mock.patch.object(server, "SUDOERS_DROPIN", self.tmp / "no-sudoers")):
            p.start()
            self.addCleanup(p.stop)
        self.app = server.get_app(cfg, str(cfg_path))
        self.sudo.calls.clear()

    def post(self, path, **fields):
        ep = _endpoint(self.app, path, "POST")
        return _out(ep(ep.__annotations__["body"](**fields)))


class Endpoints(_AppCase):

    def test_bad_addresses_are_400_before_the_installer_runs(self):
        for path, fn in (("/api/ntfy/config", "update_config"), ("/api/ntfy/install", "install")):
            with mock.patch.object(ntfy_installer, fn) as called:
                for u in BAD_URLS:
                    status, body = self.post(path, base_url=u)
                    self.assertEqual(status, 400, (path, u))
                    self.assertFalse(body["ok"])
                    self.assertEqual(body["error"], body["message"])
                    self.assertIn("http://host:port", body["message"])
                called.assert_not_called()

    def test_custom_keys_is_409_with_names_and_nothing_written(self):
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\nweb-push-file: "/srv/example/wp.db"\n')
        status, body = self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                                 upstream_relay=True)
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "custom_keys")
        self.assertEqual(body["custom_keys"], ["auth-file", "web-push-file"])
        self.assertEqual(body["error"], body["message"])
        self.assertNotIn("/srv/example", json.dumps(body))
        self.assertEqual(self.sudo.calls, [])

    def test_drop_custom_keys_passes_through(self):
        with mock.patch.object(ntfy_installer, "update_config",
                               return_value={"ok": True, "message": "m"}) as uc:
            self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                      drop_custom_keys=["auth-file"])
        self.assertEqual(uc.call_args.kwargs["drop_custom_keys"], ["auth-file"])
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        status, body = self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                                 upstream_relay=True, drop_custom_keys=["auth-file"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["dropped_keys"], ["auth-file"])
        self.assertNotIn("auth-file", self.cfg.read_text())

    def test_drop_custom_keys_is_the_409s_names_not_a_flag(self):
        """A bare true is no confirmation: the request is refused before it
        reaches update_config."""
        import pydantic
        with self.assertRaises(pydantic.ValidationError):
            self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                      drop_custom_keys=True)

    def test_a_setting_added_after_the_409_brings_a_fresh_409(self):
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        status, body = self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                                 upstream_relay=True)
        self.assertEqual((status, body["custom_keys"]), (409, ["auth-file"]))
        # Before the confirm comes back, someone locks ntfy down by hand.
        self.live(self.cfg.read_text() + 'auth-default-access: "deny-all"\n')
        status, body = self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                                 upstream_relay=True, drop_custom_keys=body["custom_keys"])
        self.assertEqual(status, 409, body)
        self.assertEqual(body["custom_keys"], ["auth-default-access", "auth-file"])
        self.assertEqual(self.sudo.calls, [])
        self.assertIn("deny-all", self.cfg.read_text())
        # Confirming the names now listed saves.
        status, body = self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                                 upstream_relay=True, drop_custom_keys=body["custom_keys"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["dropped_keys"], ["auth-default-access", "auth-file"])

    def test_stored_value_invalid_is_400_with_keys(self):
        self.live(LEGACY.replace("http://192.0.2.10:2586", "http://x?q=1"))
        status, body = self.post("/api/ntfy/config", upstream_relay=False)
        self.assertEqual(status, 400)
        self.assertEqual((body["code"], body["keys"]), ("stored_value_invalid", ["base-url"]))
        self.assertEqual(self.sudo.calls, [])

    def test_helper_errors_reach_the_page_with_their_status(self):
        """v3.4.150: no helper answered → 500 with the refresh command; the
        helper busy → 409; a download still running → 202. error == message."""
        self.live(LEGACY)
        self.sudo.unavailable = True
        status, body = self.post("/api/ntfy/config", base_url="http://192.0.2.20:2586",
                                 upstream_relay=True)
        self.assertEqual((status, body["code"]), (500, "helper_unavailable"))
        self.assertIn(body["command"], body["message"])
        self.assertEqual(body["error"], body["message"])
        self.sudo.unavailable = False
        with mock.patch.object(privhelper, "run_sudo",
                               side_effect=subprocess.TimeoutExpired(["sudo"], 300)):
            status, body = self.post("/api/ntfy/install", port=2586)
        self.assertEqual(status, 200, body)                  # already installed: no helper call
        self.STATE = "not_installed"
        with mock.patch.object(privhelper, "run_sudo",
                               side_effect=subprocess.TimeoutExpired(["sudo"], 300)), \
                mock.patch.object(ntfy_installer, "_detect_lan_ip",
                                  return_value=("192.0.2.10", None)):
            status, body = self.post("/api/ntfy/install", port=2586)
        self.assertEqual((status, body["code"], body["still_running"]), (202, "still_running", True))
        self.assertIn("Check the status again in a minute", body["message"])
        self.STATE = "aerodrome_managed"
        import fcntl
        fd = os.open(str(self.host.f("/run/aerodrome-helper.lock")), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(ntfy_installer, "install_status", side_effect=lambda: {
                    "state": "aerodrome_managed", "arch": "linux_amd64", "version": "2.10.0",
                    "stamp": {}}):
                status, body = _out(_endpoint(self.app, "/api/ntfy/upgrade", "POST")())
        finally:
            os.close(fd)
        self.assertEqual((status, body["code"]), (409, "busy"), body)

    def test_status_says_when_the_older_setup_is_still_in_use(self):
        """legacy_unit, and the command that finishes the move (with the
        names to drop, when there are any)."""
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        with mock.patch.object(ntfy_installer, "latest_version", return_value=None), \
             mock.patch.object(ntfy_installer, "_detect_lan_ip", return_value=("192.0.2.10", None)), \
             mock.patch.object(ntfy_installer, "install_status", side_effect=lambda: {
                 "state": "aerodrome_managed", "arch": "linux_amd64", "legacy_unit": True}):
            status, body = _out(_endpoint(self.app, "/api/ntfy/status", "GET")())
        self.assertEqual(status, 200)
        self.assertTrue(body["status"]["legacy_unit"])
        self.assertEqual(body["config"]["migrate_command"],
                         f"sudo {privhelper.HELPER} ntfy-migrate --drop-custom-keys auth-file")

    def test_status_reports_names_and_hides_an_invalid_address(self):
        self.live(LEGACY.replace("http://192.0.2.10:2586", "http://x?q=1")
                  + 'auth-file: "/srv/example/user.db"\n')
        with mock.patch.object(ntfy_installer, "latest_version", return_value=None), \
             mock.patch.object(ntfy_installer, "_detect_lan_ip", return_value=("192.0.2.10", None)):
            status, body = _out(_endpoint(self.app, "/api/ntfy/status", "GET")())
        self.assertEqual(status, 200)
        cfg = body["config"]
        self.assertIsNone(cfg["base_url"])
        self.assertEqual(cfg["custom_keys"], ["auth-file"])
        self.assertEqual(cfg["problems"], ["base-url"])
        self.assertNotIn("/srv/example", json.dumps(body))


class Restore(_AppCase):

    def restore(self, server_yml, size_pad=0):
        zp = self.install_dir / ".backups" / "aerodrome-backup-20260101-000000.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"manifest_version": 1}))
            zf.writestr("ntfy/server.yml", server_yml + "#" * size_pad)
        ep = _endpoint(self.app, "/api/backup/restore-from-path", "POST")
        payload = ep.__annotations__["payload"](path=str(zp))
        out = ep(payload, background_tasks=mock.Mock())
        if asyncio.iscoroutine(out):          # an async handler before v3.4.149
            out = asyncio.run(out)
        out = _out(out)
        # v3.4.147: a restore keeps the maintenance lock until its restart
        # lands; the (mocked) restart has "happened" before the next one.
        server._MAINT = server._SingleRun()
        return out

    def stash_files(self):
        return sorted(p.name for p in self.install_dir.glob("ntfy-server.yml.from-backup.*"))

    def test_managed_restore_writes_managed_keys_only(self):
        self.live(LEGACY)
        backup = (LEGACY.replace("http://192.0.2.10:2586", "http://192.0.2.30:8090")
                  .replace('"0.0.0.0:2586"', '"127.0.0.1:8090"')
                  .replace("/var/lib/ntfy/cache.db", "/srv/example/cache.db")
                  + 'auth-file: "/srv/example/user.db"\n'
                    'attachment-cache-dir: "/srv/example/attachments"\n')
        with mock.patch.object(ntfy_installer, "update_config",
                               wraps=ntfy_installer.update_config) as uc:
            status, body = self.restore(backup)
        self.assertEqual(status, 200, body)
        self.assertEqual(uc.call_args.kwargs, {
            "base_url": "http://192.0.2.30:8090", "upstream_relay": True,
            "port": 8090, "bind": "127.0.0.1", "drop_custom_keys": None})
        self.assertEqual(self.helper_calls(), [config_args(
            port=8090, bind="127.0.0.1", base_url="http://192.0.2.30:8090")])
        y = yaml.safe_load(self.cfg.read_text())
        self.assertEqual(set(y), MANAGED_KEYS - {"cache-file"})    # the unit names the cache
        self.assertEqual(y["listen-http"], "127.0.0.1:8090")
        self.assertIn(["systemctl", "restart", "ntfy"], self.host.calls)
        self.assertEqual(body["restored"]["ntfy_config"],
                         {"ok": True, "bytes": len(backup.encode()),
                          "carried": ["base-url", "listen-http", "upstream-base-url"]})
        self.assertIn("Left out notification-server settings Aerodrome doesn't manage: "
                      "attachment-cache-dir, auth-file, cache-file", body["warnings"])
        self.assertIn(OLD_SERVER_WARNING, body["warnings"])
        self.assertNotIn("/srv/example", json.dumps(body))
        self.assertEqual(self.stash_files(), [])

    def test_backup_carrying_the_injection_shape_is_contained(self):
        """A server.yml written through the old base_url hole restores
        without its injected key."""
        self.live(LEGACY)
        status, body = self.restore('base-url: "' + INJECTION + '"\n'
                                    'listen-http: "0.0.0.0:2586"\n')
        self.assertEqual(status, 200, body)
        y = yaml.safe_load(self.cfg.read_text())
        self.assertEqual(set(y), MANAGED_KEYS - {"upstream-base-url", "cache-file"})
        self.assertEqual(y["base-url"], "http://192.0.2.10:2586")
        self.assertIn("Left out notification-server settings Aerodrome doesn't manage: "
                      "auth-file", body["warnings"])

    def test_live_custom_keys_leave_the_live_file_alone(self):
        live = LEGACY + 'auth-file: "/srv/example/user.db"\nauth-default-access: "deny-all"\n'
        self.live(live)
        status, body = self.restore(LEGACY.replace("192.0.2.10", "192.0.2.30"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(self.cfg.read_text(), live)
        self.assertEqual(len(self.stash_files()), 1)
        self.assertNotIn("ntfy_config", body["restored"])
        reason = next(s["reason"] for s in body["skipped"] if s["file"] == "ntfy/server.yml")
        self.assertIn("(auth-default-access, auth-file)", reason)
        self.assertIn(self.stash_files()[0], reason)

    def test_stash_is_private(self):
        """The backup's server.yml may hold credentials: the copy restore
        saves aside is owner-only, whatever the service's umask."""
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.live(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        stash = self.install_dir / "ntfy-server.yml.from-backup.20260101-000000"
        with mock.patch.object(server.time, "strftime", return_value="20260101-000000"):
            # an older same-name file (a retried restore) is tightened too
            stash.write_text("old")
            stash.chmod(0o644)
            self.restore(LEGACY + 'upstream-access-token: "tk_example"\n')
        self.assertEqual(self.stash_files(), [stash.name])
        self.assertEqual(stat.S_IMODE(stash.stat().st_mode), 0o600)
        self.assertIn("upstream-access-token", stash.read_text())

    def test_live_hand_set_listen_address_is_not_widened(self):
        for live_listen in ('"[::1]:2586"', '"192.0.2.5:2586"', '"localhost:2586"'):
            self.sudo.calls.clear()
            for p in self.install_dir.glob("ntfy-server.yml.from-backup.*"):
                p.unlink()
            live = LEGACY.replace('"0.0.0.0:2586"', live_listen)
            self.live(live)
            status, body = self.restore(LEGACY)
            self.assertEqual(status, 200, body)
            self.assertEqual(self.sudo.calls, [], live_listen)
            self.assertEqual(self.cfg.read_text(), live)
            self.assertEqual(len(self.stash_files()), 1)
            self.assertNotIn("ntfy_config", body["restored"])
            reason = next(s["reason"] for s in body["skipped"] if s["file"] == "ntfy/server.yml")
            self.assertIn("can't carry over: listen-http", reason)
            self.assertIn(self.stash_files()[0], reason)

    def test_unusable_addresses_on_both_sides_leave_the_live_file_alone(self):
        live = LEGACY.replace("http://192.0.2.10:2586", "http://[fe80::1%eth0]:2586")
        self.live(live)
        status, body = self.restore(LEGACY.replace("http://192.0.2.10:2586", "http://x?q=1"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(self.cfg.read_text(), live)
        self.assertEqual(len(self.stash_files()), 1)
        self.assertNotIn("ntfy_config", body["restored"])
        self.assertEqual(body["warnings"], [])
        reason = next(s["reason"] for s in body["skipped"] if s["file"] == "ntfy/server.yml")
        self.assertIn("no longer accepted — re-enter it.", reason)
        self.assertIn(self.stash_files()[0], reason)

    def test_ntfy_not_coming_back_leaves_the_live_file(self):
        """The helper's health check fails after the restart: it puts the
        previous files back, so skipped[] says so and no warning claims
        anything was written."""
        self.live(LEGACY)
        before_unit = self.unit.read_text()
        self.host.healthy = False
        status, body = self.restore(LEGACY.replace("192.0.2.10", "192.0.2.30")
                                    + 'auth-file: "/srv/example/user.db"\n')
        self.assertEqual(status, 200, body)
        self.assertEqual((self.cfg.read_text(), self.unit.read_text()), (LEGACY, before_unit))
        self.assertNotIn("ntfy_config", body["restored"])
        reason = next(s["reason"] for s in body["skipped"] if s["file"] == "ntfy/server.yml")
        self.assertIn("previous settings were put back", reason)
        self.assertIn(self.stash_files()[0], reason)
        self.assertFalse([w for w in body["warnings"] if "Left out" in w])
        self.assertNotIn("/srv/example", json.dumps(body))

    def test_unusable_backup_port_warns_per_branch(self):
        backup = LEGACY.replace('"0.0.0.0:2586"', '"198.51.100.1:2586"')
        self.live(LEGACY.replace('"0.0.0.0:2586"', '"127.0.0.1:8080"'))
        status, body = self.restore(backup)
        self.assertEqual(status, 200, body)
        self.assertIn("The backup's notification server port wasn't usable, so the current "
                      "one was kept.", body["warnings"])
        self.assertEqual(yaml.safe_load(self.cfg.read_text())["listen-http"], "127.0.0.1:8080")
        self.STATE = "not_installed"
        with mock.patch.object(ntfy_installer, "install",
                               return_value={"ok": True, "message": "m"}) as inst:
            status, body = self.restore(backup)
        self.assertEqual((inst.call_args.kwargs["port"], inst.call_args.kwargs["bind"]),
                         (2586, "0.0.0.0"))
        self.assertIn("The backup's notification server port wasn't usable, so the default "
                      "(2586) was used.", body["warnings"])

    def test_fresh_install_failing_after_the_write_still_names_what_was_left_out(self):
        self.STATE = "not_installed"
        with mock.patch.object(ntfy_installer, "install", return_value={
                "ok": False, "written": True, "message": "Failed to enable/start service: x"}):
            status, body = self.restore(LEGACY + 'auth-file: "/srv/example/user.db"\n')
        self.assertEqual(status, 200, body)
        reason = next(s["reason"] for s in body["skipped"] if s["file"] == "ntfy/server.yml")
        self.assertIn("Auto-install of ntfy failed: Failed to enable/start service", reason)
        self.assertIn("Left out notification-server settings Aerodrome doesn't manage: "
                      "auth-file", body["warnings"])
        self.assertNotIn("ntfy_config", body["restored"])

    def test_crafted_long_value_restores_promptly(self):
        """A ~256 KB digit run (compresses to a few hundred bytes) used to
        hold the event loop for minutes in the number check."""
        self.live(LEGACY)
        t0 = time.perf_counter()
        status, body = self.restore(LEGACY + "x: " + "1" * 250000 + "z\n")
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(status, 200, body)
        self.assertIn("Left out notification-server settings Aerodrome doesn't manage: x",
                      body["warnings"])

    def test_not_installed_installs_with_the_backups_values(self):
        self.STATE = "not_installed"
        with mock.patch.object(ntfy_installer, "install",
                               return_value={"ok": True, "message": "m"}) as inst:
            status, body = self.restore(LEGACY.replace("http://192.0.2.10:2586",
                                                       "https://ntfy.example.net")
                                        .replace('"0.0.0.0:2586"', '"0.0.0.0:8080"')
                                        .replace('upstream-base-url: "https://ntfy.sh"\n', ""))
        self.assertEqual(status, 200, body)
        self.assertEqual(inst.call_args.kwargs, {
            "port": 8080, "bind": "0.0.0.0", "base_url": "https://ntfy.example.net",
            "upstream_relay": False})
        self.assertEqual(self.helper_calls(), [])       # install wrote it; no raw copy
        self.assertTrue(body["restored"]["ntfy_config"]["ok"])

    def test_unusable_backup_address_keeps_current_when_managed(self):
        self.live(LEGACY)
        with mock.patch.object(ntfy_installer, "update_config",
                               wraps=ntfy_installer.update_config) as uc:
            status, body = self.restore(LEGACY.replace("http://192.0.2.10:2586",
                                                       "http://[fe80::1%eth0]:2586"))
        self.assertEqual(status, 200, body)
        self.assertIsNone(uc.call_args.kwargs["base_url"])
        self.assertIn("The backup had no usable notification server address, "
                      "so the current one was kept.", body["warnings"])
        self.assertNotIn(OLD_SERVER_WARNING, body["warnings"])
        self.assertEqual(yaml.safe_load(self.cfg.read_text())["base-url"],
                         "http://192.0.2.10:2586")

    def test_unusable_backup_address_is_detected_on_a_fresh_install(self):
        self.STATE = "not_installed"
        with mock.patch.object(ntfy_installer, "install",
                               return_value={"ok": True, "message": "m"}) as inst:
            status, body = self.restore(LEGACY.replace("http://192.0.2.10:2586", "http://x?q=1"))
        self.assertEqual(status, 200, body)
        self.assertIsNone(inst.call_args.kwargs["base_url"])
        self.assertIn("The backup had no usable notification server address, "
                      "so it was detected automatically.", body["warnings"])
        self.assertNotIn(OLD_SERVER_WARNING, body["warnings"])

    def test_oversized_server_yml_is_skipped(self):
        """Guard (same on v3.4.144)."""
        self.live(LEGACY)
        status, body = self.restore(LEGACY, size_pad=300 * 1024)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo.calls, [])
        self.assertTrue(any("improbably large" in s["reason"] for s in body["skipped"]))

    def test_external_install_is_never_touched(self):
        """Guard (same on v3.4.144)."""
        self.STATE = "external"
        status, body = self.restore(LEGACY)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.sudo.calls, [])
        self.assertEqual(len(self.stash_files()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
