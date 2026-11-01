"""scripts/aerodrome-helper — the one program the service account may run as
root (v3.4.150). Stdlib unittest; no root, no network, nothing outside a temp
dir: the helper is loaded as a module with ROOT pointing at a fake root, RUN
answering for systemd (it tracks which units are active and enabled,
restart counts and failures, and which unit text the running ntfy was
started from), PROBE standing in for ntfy's health endpoint,
FETCH serving a fixture release, a fake CLOCK only SLEEP moves on, and
_geteuid pretending to be root.

Also the fake-root harness other test files use (HelperHost, and
HelperSudo: a subprocess.run stand-in that runs `sudo -n <HELPER> …` with
the real helper on a fake root). Runs under the oldest Python the helper
supports (3.10) as well as the current one. Addresses are RFC 5737 /
example.net; account and path names are invented.
"""
import ast
import contextlib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent
HELPER_SRC = REPO / "scripts" / "aerodrome-helper"
ELF = b"\x7fELF" + b"\0" * 60 + b"fixture-ntfy"

LEGACY_UNIT = """[Unit]
Description=ntfy server (managed by Aerodrome)
After=network.target

[Service]
ExecStart=/usr/local/bin/ntfy serve --config /etc/ntfy/server.yml
Restart=on-failure
RestartSec=5
# Run as root by default to allow binding to low ports if reconfigured.
# The config file itself restricts what ntfy actually does.

[Install]
WantedBy=multi-user.target
"""
# What v3.4.144 and earlier wrote (comments trimmed).
LEGACY_CFG = '''# Aerodrome-managed ntfy config. Edit at your own risk
base-url: "http://192.0.2.10:2586"
listen-http: "0.0.0.0:2586"
cache-file: /var/lib/ntfy/cache.db
cache-duration: 12h
behind-proxy: false
upstream-base-url: "https://ntfy.sh"
'''
NTFY_ARGS = ["--port", "2586", "--bind", "0.0.0.0", "--base-url", "http://192.0.2.10:2586",
             "--upstream", "1"]


class Killed(BaseException):
    """A simulated SIGKILL: not an Exception, so no handler in the helper sees it."""


def load_helper():
    """A fresh copy of the helper as a module (each test gets its own)."""
    loader = importlib.machinery.SourceFileLoader("aerodrome_helper_under_test", str(HELPER_SRC))
    spec = importlib.util.spec_from_loader("aerodrome_helper_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def make_tarball(ver, arch="linux_amd64", member=ELF, name=None, symlink=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name or f"ntfy_{ver}_{arch}/ntfy")
        if symlink:
            ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/hostname"
            tf.addfile(ti)
        else:
            ti.size, ti.mode = len(member), 0o755
            tf.addfile(ti, io.BytesIO(member))
    return buf.getvalue()


class HelperHost:
    """A fake root and the systemd the helper talks to. `active`: units
    running; `enabled`: units enabled; `healthy`: what ntfy's health
    endpoint answers; `crashloop`: ntfy keeps answering (someone else on the
    port) while its restart count climbs; `fail_restart`: units whose
    (re)start job fails; `now`: the helper's clock (SLEEP moves it on).
    `loaded`: the ntfy unit text systemd read at its last daemon-reload;
    `running_def`: the one the running ntfy was started from — as with
    systemd, a start of a unit that is already active changes nothing."""

    def __init__(self, root):
        self.root = Path(root)
        for d in ("etc/systemd/system", "etc/ntfy", "usr/local/bin", "usr/bin", "run",
                  "var/lib/private", "var/lib/ntfy", "var/cache", "lib/systemd/system",
                  "usr/lib/systemd/system"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.calls, self.active, self.enabled, self.healthy = [], set(), set(), True
        self.crashloop, self.restarts, self.fail_restart = False, 0, set()
        self.loaded = self.running_def = None
        self.release, self.fetches = {}, []
        h = self.h = load_helper()
        h.ROOT, h.EXPECTED_OWNER_UID = str(self.root), os.getuid()
        h._geteuid, h._caller_gids, h._arch = (lambda: 0), (lambda: set()), (lambda: "linux_amd64")
        self.now = 0.0
        h.SLEEP, h.CLOCK, h.HEALTH_TIMEOUT, h.SETTLE, h.LOCK_WAIT = (
            self.sleep, (lambda: self.now), 4.0, 1.0, 1.0)
        h.RUN, h.PROBE, h.FETCH = self.run, (lambda port, timeout=2.0: self.healthy), self.fetch
        self.serve_release("2.11.0")

    # -- systemd, the network and the clock ----------------------------------
    def sleep(self, s):
        self.now += s

    def run(self, argv, timeout=60):
        self.calls.append(list(argv))
        if argv[0] != "systemctl":
            raise AssertionError(f"the helper ran {argv}")
        verb, unit = argv[1], argv[-1]
        if verb == "is-active":
            return (0 if unit in self.active else 3), ""
        if verb == "show":
            if self.crashloop:
                self.restarts += 1
            state = "active" if unit in self.active else "inactive"
            return 0, f"ActiveState={state}\nNRestarts={self.restarts}"
        if verb == "daemon-reload":
            u = self.f("/etc/systemd/system/ntfy.service")
            self.loaded = u.read_text() if u.exists() else None
        if verb == "enable":
            self.enabled.add(unit)
        if verb == "restart" or argv[1:3] == ["enable", "--now"]:
            if unit in self.fail_restart:
                self.active.discard(unit)
                if unit == "ntfy":
                    self.running_def = None
                return 1, f"Job for {unit} failed"
            if unit == "ntfy" and (verb == "restart" or unit not in self.active):
                self.running_def = self.loaded
            self.active.add(unit)
        if argv[1:3] == ["disable", "--now"]:
            self.active.discard(unit)
            self.enabled.discard(unit)
            if unit == "ntfy":
                self.running_def = None
        return 0, ""

    def serve_release(self, ver, tarball=None, digest=None, tag=None):
        name = f"ntfy_{ver}_linux_amd64.tar.gz"
        blob = tarball if tarball is not None else make_tarball(ver)
        meta = json.dumps({"tag_name": tag or f"v{ver}", "assets": [
            {"name": name, "digest": digest or "sha256:" + hashlib.sha256(blob).hexdigest()}]})
        api = "https://api.github.com/repos/binwiederhier/ntfy/releases"
        self.release[f"{api}/latest"] = meta.encode()
        self.release[f"{api}/tags/v{ver}"] = meta.encode()
        self.release[f"https://github.com/binwiederhier/ntfy/releases/download/v{ver}/{name}"] = blob

    def fetch(self, url, limit):
        self.fetches.append(url)
        if url not in self.release:
            raise AssertionError(f"unexpected fetch {url}")
        return self.release[url]

    # -- files -----------------------------------------------------------------
    def f(self, path):
        return self.root / path.lstrip("/")

    def legacy_host(self, cfg=LEGACY_CFG, unit=LEGACY_UNIT, running=True):
        """An install made by v3.4.144 or earlier: root-run unit, stamp."""
        self.f("/etc/systemd/system/ntfy.service").write_text(unit)
        self.f("/etc/ntfy/server.yml").write_text(cfg)
        self.f("/var/lib/ntfy/aerodrome-installed").write_text("2.10.0\n")
        self.f("/usr/local/bin/ntfy").write_bytes(ELF)
        self.loaded = unit
        if running:
            self.active.add("ntfy")
            self.enabled.add("ntfy")
            self.running_def = unit

    def pair(self):
        u, c = self.f("/etc/systemd/system/ntfy.service"), self.f("/etc/ntfy/server.yml")
        return (u.read_text() if u.exists() else None), (c.read_text() if c.exists() else None)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and "run" not in p.parts}

    def main(self, *argv):
        out = io.StringIO()
        rc = self.h.main(list(argv), out=out)
        lines = out.getvalue().splitlines()
        if len(lines) != 1:
            raise AssertionError(f"not one JSON line: {out.getvalue()!r}")
        return rc, json.loads(lines[0])


class HelperSudo:
    """subprocess.run stand-in for app tests: `sudo -n <privhelper.HELPER> sub
    …` runs the real helper in-process on a HelperHost's fake root (its
    systemctl calls go to the host's recorder); `unavailable` makes it answer
    the way sudo does with no rule. A plain `systemctl` (the app's status
    reads) reports inactive. Anything else is a test failure."""

    def __init__(self, host, helper_path):
        self.host, self.helper_path = host, str(helper_path)
        self.calls, self.unavailable = [], False

    def helper_calls(self):
        return [c[3:] for c in self.calls if c[:3] == ["sudo", "-n", self.helper_path]]

    def __call__(self, cmd, *a, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["sudo", "-n", self.helper_path]:
            if self.unavailable:
                return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required\n")
            out = io.StringIO()
            rc = self.host.h.main(cmd[3:], out=out)
            return subprocess.CompletedProcess(cmd, rc, out.getvalue(), "")
        if cmd[:1] == ["systemctl"]:
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        raise AssertionError(f"unexpected command {cmd}")


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="aerodrome-helper-test-")
        self.addCleanup(tmp.cleanup)
        self.host = HelperHost(tmp.name)
        self.h = self.host.h
        self.f, self.main = self.host.f, self.host.main

    def assert_startable(self):
        """The pair on disk is one systemd can start: the new unit only with a
        config it can run (no custom keys), the old root-run unit with any."""
        unit, cfg = self.host.pair()
        self.assertIsNotNone(unit)
        self.assertIsNotNone(cfg)
        if unit == self.h.NTFY_UNIT_TEXT:
            self.assertEqual(self.h.classify_server_yml(cfg)["custom"], [],
                             "the new unit with a config it can't run")
            self.assertEqual(self.f("/etc/ntfy/server.yml").stat().st_mode & 0o777, 0o644)
        else:
            self.assertEqual(unit, LEGACY_UNIT)

    def assert_runs_the_unit_on_disk(self, msg=None):
        """A running ntfy was (re)started from the unit file on disk, not left
        on the one it ran before (a start of a running unit changes nothing)."""
        if "ntfy" in self.host.active:
            self.assertEqual(self.host.running_def, self.host.pair()[0], msg)


class Validators(_Case):

    def test_ports(self):
        for ok in ("1024", "2586", "65535"):
            self.assertEqual(self.h.v_port(ok), int(ok))
        for bad in ("1023", "80", "0", "65536", "", "80 ", "8080\n", "+8080", "0x50", "8e3",
                    "２５８６"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.h.v_port(bad)
        self.assertEqual(self.h.v_ntfy_port("80"), 80)       # low ports: the unit's capability
        for bad in ("0", "65536", "80\n", "-1"):
            with self.subTest(ntfy_bad=bad), self.assertRaises(Exception):
                self.h.v_ntfy_port(bad)

    def test_versions(self):
        for ok in ("2.21.0", "latest", "10.0.123"):
            self.assertEqual(self.h.v_version(ok), ok)
        for bad in ("2.21", "v2.21.0", "2.21.0;id", "../../x", "2.21.0\n", "", "1234.0.0"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.h.v_version(bad)

    def test_setting_names(self):
        self.assertEqual(self.h.v_setting_names("auth-file,line 7"), {"auth-file", "line 7"})
        self.assertEqual(self.h.v_setting_names(""), frozenset())
        for bad in ("auth-file,,x", "a b", "auth-file;id", "line x", "../x", "a\nb", "x" * 200):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.h.v_setting_names(bad)

    def test_bad_values_never_reach_a_write(self):
        self.host.legacy_host()
        before = self.host.snapshot()
        for flag, value in (("--base-url", 'http://x"\nauth-file: "/srv/example/u.db'),
                            ("--base-url", "http://[fe80::1%eth0]:2586"), ("--bind", "0.0.0.0\nx: y"),
                            ("--bind", "::"), ("--upstream", "yes"), ("--port", "0")):
            args = dict(zip(NTFY_ARGS[::2], NTFY_ARGS[1::2]))
            args[flag] = value
            rc, out = self.main("ntfy-config", *[x for kv in args.items() for x in kv])
            self.assertEqual((rc, out["ok"]), (2, False), (flag, value))
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.host.calls, [])


class Usage(_Case):
    """Usage errors are the one JSON line too (exit 2): the app tells a
    helper that answered from one that didn't by that line."""

    def test_usage_errors_print_one_json_line(self):
        for argv in (["bogus"], [], ["ntfy-uninstall", "--purg"], ["ntfy-uninstall", "--pu"],
                     ["feeder-set-port"], ["ntfy-install"], ["version", "--x"], ["-h"],
                     ["ntfy-migrate", "--drop-custom-keys"], ["ntfy-migrate", "--drop-custom-keys", "a b"],
                     ["@argsfile"]):
            with self.subTest(argv=argv):
                rc, out = self.main(*argv)
                self.assertEqual(rc, 2)
                self.assertFalse(out["ok"])
                self.assertTrue(out["message"])
        self.assertEqual(self.host.calls, [])

    def test_not_root(self):
        self.h._geteuid = lambda: 1000
        rc, out = self.main("feeder-remove")
        self.assertEqual((rc, out["ok"]), (1, False))
        self.assertIn("root", out["message"])
        self.assertEqual(self.host.calls, [])

    def test_version_needs_no_root_and_no_lock(self):
        self.h._geteuid = lambda: 1000
        self.host.legacy_host()
        fd = os.open(str(self.f("/run/aerodrome-helper.lock")), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            rc, out = self.main("version")
        finally:
            os.close(fd)
        self.assertEqual((rc, out["version"], out["ntfy_unit"], out["ntfy_cache"]),
                         (0, 6, "legacy", False))
        self.f("/var/lib/private/aerodrome-ntfy").mkdir()
        self.f("/var/lib/private/aerodrome-ntfy/cache.db-wal").write_bytes(b"x")
        self.f("/etc/systemd/system/ntfy.service").write_text(self.h.NTFY_UNIT_TEXT)
        self.assertEqual(self.main("version")[1]["ntfy_cache"], True)
        self.assertEqual(self.main("version")[1]["ntfy_unit"], "current")


class Coupling(_Case):
    """ntfy.service and server.yml are written together, by _converge only."""

    def test_R1_upgrade_on_a_custom_key_host_never_touches_unit_or_config(self):
        self.host.legacy_host(LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n')
        before = self.host.pair()
        rc, out = self.main("ntfy-install", "latest")
        self.assertEqual((rc, out["version"], out["restarted"]), (0, "2.11.0", True), out)
        self.assertEqual(self.host.pair(), before)
        self.assertNotIn(["systemctl", "daemon-reload"], self.host.calls)
        self.assertEqual(self.f("/usr/local/bin/ntfy").read_bytes(), ELF)
        self.assertEqual(self.f("/var/lib/ntfy/aerodrome-installed").read_text(), "2.11.0\n")

    def test_R2_config_on_a_legacy_host_writes_the_new_pair(self):
        self.host.legacy_host()
        rc, out = self.main("ntfy-config", *NTFY_ARGS)
        self.assertEqual(rc, 0, out)
        unit, cfg = self.host.pair()
        self.assertEqual(unit, self.h.NTFY_UNIT_TEXT)
        self.assertNotIn("cache-file", cfg)
        self.assertIn("--cache-file /var/lib/aerodrome-ntfy/cache.db", unit)
        old = self.f("/etc/ntfy/server.yml.pre-v6")
        self.assertEqual((old.read_text(), old.stat().st_mode & 0o777), (LEGACY_CFG, 0o600))
        self.assertIn(["systemctl", "restart", "ntfy"], self.host.calls)

    def test_custom_keys_refuse_until_every_one_is_named(self):
        cfg = LEGACY_CFG + 'auth-file: "/srv/example/user.db"\nauth-default-access: "deny-all"\n'
        self.host.legacy_host(cfg)
        before = self.host.snapshot()
        for drop in (None, "", "auth-file", "auth-default-access,web-push-file"):
            with self.subTest(drop=drop):
                extra = [] if drop is None else ["--drop-custom-keys", drop]
                rc, out = self.main("ntfy-config", *NTFY_ARGS, *extra)
                self.assertEqual((rc, out["custom_keys"]), (3, ["auth-default-access", "auth-file"]))
                self.assertNotIn("/srv/example", json.dumps(out))      # names, never values
                self.assertEqual(self.host.snapshot(), before)
        rc, out = self.main("ntfy-migrate", "--drop-custom-keys", "auth-file")
        self.assertEqual((rc, out["custom_keys"]), (3, ["auth-default-access", "auth-file"]))
        self.assertEqual(self.host.snapshot(), before)
        rc, out = self.main("ntfy-config", *NTFY_ARGS, "--drop-custom-keys",
                            "auth-default-access,auth-file,web-push-file")   # extra names are harmless
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.f("/etc/ntfy/server.yml.pre-v6").read_text(), cfg)
        self.assertNotIn("auth", self.host.pair()[1])

    def test_pre_v6_keeps_the_file_each_drop_removed_settings_from(self):
        """After a rolled-back move, a later drop still keeps the file it
        changes (not only the first one)."""
        self.host.legacy_host()
        self.host.healthy = False
        self.assertEqual(self.main("ntfy-migrate")[0], 1)            # rolled back
        self.assertEqual(self.f("/etc/ntfy/server.yml.pre-v6").read_text(), LEGACY_CFG)
        edited = LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n'
        self.f("/etc/ntfy/server.yml").write_text(edited)
        self.host.healthy = True
        rc, out = self.main("ntfy-migrate", "--drop-custom-keys", "auth-file")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.f("/etc/ntfy/server.yml.pre-v6").read_text(), edited)
        self.assertEqual(self.f("/etc/ntfy/server.yml.pre-v6").stat().st_mode & 0o777, 0o600)

    def test_R3_operator_values_carry_over_or_refuse(self):
        self.host.legacy_host(LEGACY_CFG.replace("192.0.2.10", "adsb_pi.lan"))
        self.assertEqual(self.main("ntfy-migrate")[0], 0)
        self.assertIn('"http://adsb_pi.lan:2586"', self.host.pair()[1])
        for cfg, field, names in (
                (LEGACY_CFG.replace("behind-proxy: false", "behind-proxy: true"),
                 "custom_keys", ["behind-proxy"]),
                (LEGACY_CFG.replace("192.0.2.10:2586", "[fe80::1%eth0]:2586"),
                 "problems", ["base-url"])):
            with self.subTest(field=field):
                self.setUp()
                self.host.legacy_host(cfg)
                before = self.host.snapshot()
                rc, out = self.main("ntfy-migrate", "--drop-custom-keys", "behind-proxy")
                if field == "problems":                 # the flag never overrides a problem
                    self.assertEqual((rc, out["problems"]), (3, names))
                    self.assertEqual(self.host.snapshot(), before)
                    self.assertNotIn("localhost", self.host.pair()[1])
                else:
                    self.assertEqual(rc, 0, out)
                    rc, out = self.main("ntfy-migrate")
                    self.assertEqual(rc, 0, out)      # already moved: nothing left to drop
                self.setUp()
                self.host.legacy_host(cfg)
                rc, out = self.main("ntfy-migrate")
                self.assertEqual((rc, out[field]), (3, names))

    def test_migrate_is_idempotent(self):
        self.host.legacy_host()
        self.assertEqual(self.main("ntfy-migrate")[1]["migrated"], True)
        snap = self.host.snapshot()
        self.host.calls.clear()
        rc, out = self.main("ntfy-migrate")
        self.assertEqual((rc, out["migrated"]), (0, False))
        self.assertEqual(self.host.snapshot(), snap)
        self.assertNotIn(["systemctl", "restart", "ntfy"], self.host.calls)

    def test_a_moved_server_is_left_alone_whatever_its_settings(self):
        """Settings added by hand after the move are the Notifications tab's
        business: the refresh (ntfy-migrate) doesn't ask to drop them, and a
        drop command written for them changes nothing either."""
        self.host.legacy_host()
        self.assertEqual(self.main("ntfy-migrate")[1]["migrated"], True)
        with self.f("/etc/ntfy/server.yml").open("a") as f:
            f.write('visitor-request-limit-burst: "60"\n')
        snap = self.host.snapshot()
        self.host.calls.clear()
        for extra in ([], ["--drop-custom-keys", "visitor-request-limit-burst"]):
            with self.subTest(extra=extra):
                self.assertEqual(self.main("ntfy-migrate", *extra),
                                 (0, {"ok": True, "migrated": False, "already": True}))
        self.assertEqual(self.host.snapshot(), snap)
        self.assertEqual(self.f("/etc/ntfy/server.yml.pre-v6").read_text(), LEGACY_CFG)
        self.assertNotIn(["systemctl", "restart", "ntfy"], self.host.calls)

    def test_a_server_with_no_unit_is_started_when_it_moves(self):
        """A stamped install with its binary and config but no unit (cut
        short before v3.4.150): the move writes the unit, enables and starts
        it, and checks that ntfy answers."""
        self.host.legacy_host(running=False)
        self.f("/etc/systemd/system/ntfy.service").unlink()
        rc, out = self.main("ntfy-migrate")
        self.assertEqual((rc, out["migrated"], out["restarted"]), (0, True, True), out)
        self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
        self.assertIn("ntfy", self.host.enabled)
        self.assertIn("ntfy", self.host.active)

    def test_migrate_removes_the_service_accounts_staging_folder(self):
        self.host.legacy_host()
        staging = self.f("/var/cache/ntfy/staging")
        staging.mkdir(parents=True)
        (staging / "ntfy").write_bytes(b"planted")
        self.assertEqual(self.main("ntfy-migrate")[0], 0)
        self.assertFalse(self.f("/var/cache/ntfy").exists())

    def test_migrate_is_a_no_op_when_not_ours(self):
        self.assertEqual(self.main("ntfy-migrate"), (0, {"ok": True, "migrated": False}))
        self.assertEqual(self.host.calls, [])

    def test_config_restarts_only_when_running(self):
        self.host.legacy_host(running=False)
        self.assertEqual(self.main("ntfy-config", *NTFY_ARGS)[1]["restarted"], False)
        self.assertNotIn(["systemctl", "restart", "ntfy"], self.host.calls)
        self.host.active.add("ntfy")
        args = list(NTFY_ARGS)
        args[1] = "2590"
        rc, out = self.main("ntfy-config", *args)
        self.assertEqual((rc, out["restarted"]), (0, True), out)
        self.assertIn('listen-http: "0.0.0.0:2590"', self.host.pair()[1])


class StampedAndExternal(_Case):

    def test_a_stamped_host_with_a_hand_added_setting_and_no_unit_is_refused(self):
        """B5a left install()'s 'partial' state as is: a direct POST
        /api/ntfy/install reaches it. The helper refuses before downloading
        anything; server.yml stays byte for byte."""
        self.f("/var/lib/ntfy/aerodrome-installed").write_text("2.10.0\n")
        cfg = LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n'
        self.f("/etc/ntfy/server.yml").write_text(cfg)
        before = self.host.snapshot()
        rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual((rc, out["custom_keys"]), (3, ["auth-file"]))
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.host.fetches, [])

    def test_a_stamped_host_with_its_unit_but_no_binary_gets_the_binary_only(self):
        self.host.legacy_host(LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n', running=False)
        self.f("/usr/local/bin/ntfy").unlink()
        before = self.host.pair()
        rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.host.pair(), before)
        self.assertEqual(self.f("/usr/local/bin/ntfy").read_bytes(), ELF)

    def test_a_server_yml_without_the_stamp_is_someone_elses(self):
        self.f("/etc/ntfy/server.yml").write_text(LEGACY_CFG)
        before = self.host.snapshot()
        for argv in (["ntfy-install", "latest", *NTFY_ARGS], ["ntfy-config", *NTFY_ARGS],
                     ["ntfy-uninstall", "--purge"]):
            with self.subTest(argv=argv[0]):
                self.assertEqual(self.main(*argv)[0], 3)
        self.assertEqual(self.main("ntfy-migrate")[1], {"ok": True, "migrated": False})
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.host.fetches, [])

    def test_an_official_package_layout_is_never_touched(self):
        self.f("/usr/bin/ntfy").write_bytes(ELF)
        self.f("/lib/systemd/system/ntfy.service").write_text("[Service]\n")
        self.f("/etc/ntfy/server.yml").write_text("base-url: http://192.0.2.10\n")
        self.f("/var/cache/ntfy").mkdir()
        self.f("/var/cache/ntfy/cache.db").write_bytes(b"theirs")
        before = self.host.snapshot()
        for argv in (["ntfy-install", "latest", *NTFY_ARGS], ["ntfy-config", *NTFY_ARGS],
                     ["ntfy-uninstall", "--purge"]):
            with self.subTest(argv=argv[0]):
                rc, out = self.main(*argv)
                self.assertEqual(rc, 3, out)
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual(self.host.calls, [])
        for vendor in ("/usr/bin/ntfy", "/usr/lib/systemd/system/ntfy.service"):
            with self.subTest(vendor_only=vendor):
                self.setUp()
                self.f(vendor).write_bytes(ELF)
                self.assertEqual(self.h._ntfy_state(), "external")


class Install(_Case):

    def test_fresh_install(self):
        rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual(rc, 0, out)
        self.assertEqual((out["version"], out["restarted"]), ("2.11.0", True))
        self.assertEqual(self.f("/usr/local/bin/ntfy").read_bytes(), ELF)
        self.assertEqual(self.f("/usr/local/bin/ntfy").stat().st_mode & 0o777, 0o755)
        unit, cfg = self.host.pair()
        self.assertEqual(unit, self.h.NTFY_UNIT_TEXT)
        self.assertIn("DynamicUser=yes", unit)
        self.assertNotRegex(unit, r"(?m)^User=")
        self.assertIn("CapabilityBoundingSet=CAP_NET_BIND_SERVICE", unit)
        c = self.h.classify_server_yml(cfg)
        self.assertEqual((c["base_url"], c["port"], c["bind"], c["upstream"], c["custom"],
                          c["problems"]), ("http://192.0.2.10:2586", 2586, "0.0.0.0", True, [], []))
        self.assertEqual(self.f("/var/lib/ntfy/aerodrome-installed").read_text(), "2.11.0\n")
        enable = self.host.calls.index(["systemctl", "enable", "ntfy"])
        self.assertIn(["systemctl", "restart", "ntfy"], self.host.calls[enable:])
        self.assertEqual((self.host.enabled, self.host.active), ({"ntfy"}, {"ntfy"}))
        self.assertEqual(self.host.running_def, self.h.NTFY_UNIT_TEXT)
        self.assertFalse(self.f("/etc/ntfy/server.yml.pre-v6").exists())
        leftovers = [p for p in self.host.root.rglob(".aerodrome-helper.*")]
        self.assertEqual(leftovers, [])

    def test_a_pinned_version(self):
        self.host.serve_release("2.12.0")
        rc, out = self.main("ntfy-install", "2.12.0", *NTFY_ARGS)
        self.assertEqual((rc, out["version"]), (0, "2.12.0"), out)

    def test_a_pinned_version_only_on_the_restricted_setup(self):
        """The older unit runs ntfy as root: there the caller gets the latest
        release only (refused before any download); once moved, any."""
        self.host.serve_release("1.0.0")
        self.host.legacy_host(LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n')
        before = self.host.snapshot()
        rc, out = self.main("ntfy-install", "1.0.0")
        self.assertEqual(rc, 3, out)
        self.assertIn("root-run", out["message"])
        self.assertEqual(self.host.snapshot(), before)
        self.assertEqual((self.host.fetches, self.host.calls), ([], []))
        self.assertEqual(self.main("ntfy-install", "latest")[0], 0)     # the latest: fine
        self.setUp()
        self.host.serve_release("1.0.0")
        self.host.legacy_host()
        self.assertEqual(self.main("ntfy-migrate")[1]["migrated"], True)
        rc, out = self.main("ntfy-install", "1.0.0")
        self.assertEqual((rc, out["version"]), (0, "1.0.0"), out)

    def test_a_fresh_install_needs_the_four_settings(self):
        rc, out = self.main("ntfy-install", "latest")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.host.fetches, [])
        self.assertFalse(self.f("/var/lib/ntfy/aerodrome-installed").exists())

    def test_upgrade_restarts_only_when_running(self):
        self.host.legacy_host(running=False)
        self.assertEqual(self.main("ntfy-install", "latest")[1]["restarted"], False)
        self.assertNotIn(["systemctl", "restart", "ntfy"], self.host.calls)

    def test_the_repair_runs_ntfy_on_the_unit_it_wrote(self):
        """The repair the app offers (Install) for a stamped install whose
        server.yml went missing writes the pair and starts ntfy. One still
        running on the older root-run unit is restarted on the sandboxed unit
        just written — a start alone would leave it running as root behind
        it while the reply says restarted. One that was stopped is started."""
        for running in (True, False):
            with self.subTest(running=running):
                self.setUp()
                self.host.legacy_host(running=running)
                self.f("/etc/ntfy/server.yml").unlink()
                rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
                self.assertEqual((rc, out["changed"], out["restarted"]), (0, True, True), out)
                self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
                self.assertEqual(self.host.running_def, self.h.NTFY_UNIT_TEXT,
                                 "ntfy still runs from the root-run unit")
                self.assertEqual(({"ntfy"} & self.host.enabled, {"ntfy"} & self.host.active),
                                 ({"ntfy"}, {"ntfy"}))
                self.assertEqual(self.main("version")[1]["ntfy_unit"], "current")

    def test_bad_downloads_write_nothing(self):
        cases = {
            "checksum": dict(digest="sha256:" + "0" * 64),
            "symlink member": dict(tarball=make_tarball("2.11.0", symlink=True)),
            "wrong member": dict(tarball=make_tarball("2.11.0", name="ntfy")),
            "not ELF": dict(tarball=make_tarball("2.11.0", member=b"#!/bin/sh\nid\n")),
            "traversal": dict(tarball=make_tarball("2.11.0", name="../../etc/x")),
            "tag mismatch": dict(tag="v1.0.0"),
            "no digest": dict(digest="md5:abc"),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                self.setUp()
                self.host.release.clear()
                self.host.serve_release("2.11.0", **kw)
                version = "2.11.0" if label == "tag mismatch" else "latest"
                rc, out = self.main("ntfy-install", version, *NTFY_ARGS)
                self.assertEqual(rc, 1, out)
                self.assertFalse(self.f("/usr/local/bin/ntfy").exists())
                self.assertFalse(self.f("/var/lib/ntfy/aerodrome-installed").exists())
                self.assertFalse(self.f("/etc/x").exists())

    def test_untrusted_folders_are_refused(self):
        victim = self.host.root / "victim"
        victim.mkdir()
        self.f("/etc/ntfy").rmdir()
        self.f("/etc/ntfy").symlink_to(victim)
        rc, _ = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual(rc, 1)
        self.assertEqual(list(victim.iterdir()), [])

    def test_a_folder_the_caller_can_write_is_refused(self):
        d = self.f("/usr/local/bin")
        os.chmod(d, 0o775)
        self.h._caller_gids = lambda: {d.stat().st_gid}
        rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual(rc, 1, out)
        self.assertIn("writable by the caller", out["message"])
        self.assertFalse(self.f("/usr/local/bin/ntfy").exists())
        self.setUp()
        os.chmod(self.f("/usr/local"), 0o777)           # an ancestor: world-writable
        rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual(rc, 1, out)
        self.setUp()
        os.chmod(self.f("/usr/local/bin"), 0o775)      # root:staff style, caller not in it
        self.assertEqual(self.main("ntfy-install", "latest", *NTFY_ARGS)[0], 0)


class Health(_Case):
    """After a (re)start, ntfy itself must answer its health check and keep
    answering, unrestarted, for SETTLE seconds."""

    def test_unhealthy_after_a_move_puts_the_old_files_back(self):
        """…byte for byte, the config with its own mode and owner (one that
        held a secret stays unreadable to others)."""
        self.host.legacy_host(LEGACY_CFG + 'smtp-sender-pass: "example-secret"\n')
        cfg = self.f("/etc/ntfy/server.yml")
        cfg.chmod(0o600)
        owner = (cfg.stat().st_uid, cfg.stat().st_gid)
        self.host.healthy = False
        rc, out = self.main("ntfy-migrate", "--drop-custom-keys", "smtp-sender-pass")
        self.assertEqual(rc, 1)
        self.assertIn("put back", out["message"])
        self.assertEqual(self.host.pair(),
                         (LEGACY_UNIT, LEGACY_CFG + 'smtp-sender-pass: "example-secret"\n'))
        self.assertEqual((cfg.stat().st_mode & 0o777, cfg.stat().st_uid, cfg.stat().st_gid),
                         (0o600,) + owner)
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
        self.assertEqual(self.host.calls[-2:], [["systemctl", "daemon-reload"],
                                                ["systemctl", "restart", "ntfy"]])

    def test_a_port_someone_else_answers_on_is_not_health(self):
        """V3: http.server holds the new port. The unit reads active right
        after the restart (Type=simple) and the port accepts connections, but
        ntfy isn't the one answering, and its restart count climbs."""
        self.host.legacy_host()
        self.host.crashloop = True                       # healthy probe, restarts climbing
        args = list(NTFY_ARGS)
        args[1] = "2590"
        rc, out = self.main("ntfy-config", *args)
        self.assertEqual(rc, 1, out)
        self.assertIn("put back", out["message"])
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))

    def test_a_failed_restart_job_rolls_back_too(self):
        self.host.legacy_host()
        self.host.fail_restart.add("ntfy")
        rc, out = self.main("ntfy-migrate")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))

    def test_settles_before_saying_healthy(self):
        self.host.legacy_host()
        shows = []
        real = self.host.run

        def run(argv, timeout=60):
            if argv[1] == "show":
                shows.append(argv)
            return real(argv, timeout)
        self.h.RUN = run
        self.assertEqual(self.main("ntfy-migrate")[0], 0)
        self.assertGreaterEqual(len(shows), 1 + int(self.h.SETTLE / 0.5))

    def test_the_wait_is_wall_clock_time_slow_probes_included(self):
        """A listener that accepts and never answers costs each probe its
        whole timeout: the wait still ends at HEALTH_TIMEOUT (it used to
        count only its sleeps — about five times as long)."""
        self.host.legacy_host()
        timeouts = []

        def probe(port, timeout):
            timeouts.append(timeout)
            self.host.now += timeout
            return False
        self.h.PROBE = probe
        self.assertFalse(self.h._wait_healthy(2586))
        self.assertLessEqual(self.host.now, self.h.HEALTH_TIMEOUT + 0.5)
        self.assertTrue(timeouts and all(0 < t <= 2.0 for t in timeouts), timeouts)

    def test_a_fresh_install_that_doesnt_answer_keeps_its_files(self):
        self.host.healthy = False
        rc, out = self.main("ntfy-install", "latest", *NTFY_ARGS)
        self.assertEqual(rc, 1)
        self.assertIn("isn't answering", out["message"])
        self.assertTrue(self.f("/var/lib/ntfy/aerodrome-installed").exists())
        self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)

    def test_an_unhealthy_upgrade_is_reported_not_rolled_back(self):
        self.host.legacy_host()
        self.host.healthy = False
        rc, out = self.main("ntfy-install", "latest")
        self.assertEqual(rc, 1, out)
        self.assertIn("isn't answering", out["message"])
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))


class _Kill:
    """Run the helper, killed (Killed) right after its Nth write or systemd
    step. Returns whether it got that far."""

    def run_killed(self, argv, n, healthy=True, count_fn="_atomic_write"):
        h, count = self.h, {"n": 0}
        real_w, real_s = h._atomic_write, h._systemctl
        self.host.healthy = healthy

        def tick():
            count["n"] += 1
            if count["n"] == n:
                raise Killed()

        def w(*a, **k):
            r = real_w(*a, **k)
            tick()
            return r

        def s(*a, **k):
            r = real_s(*a, **k)
            if a[0] in ("daemon-reload", "restart", "enable"):
                tick()
            return r
        h._atomic_write, h._systemctl = w, s
        try:
            with contextlib.suppress(Killed):
                self.main(*argv)
        finally:
            h._atomic_write, h._systemctl = real_w, real_s
        return count["n"] >= n


class KillPoints(_Kill, _Case):
    """A run killed after any write or systemd step leaves a pair that
    starts, and running ntfy-migrate (the documented recovery) converges —
    also when that recovery run is killed in turn. Each runs twice: a plain
    move, and one that drops a setting Aerodrome doesn't manage (there only
    the write orders keep the new unit away from that config)."""

    CUSTOM = LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n'
    CASES = ((LEGACY_CFG, ["ntfy-migrate"]),
             (CUSTOM, ["ntfy-migrate", "--drop-custom-keys", "auth-file"]))

    def assert_moved(self, n):
        self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT, n)
        self.assertEqual(self.h.classify_server_yml(self.host.pair()[1])["custom"], [], n)
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists(), n)
        self.assertIn("ntfy", self.host.active, n)
        self.assert_runs_the_unit_on_disk(n)

    def test_every_forward_kill_point(self):
        for cfg, argv in self.CASES:
            hits = 0
            for n in range(1, 15):
                with self.subTest(cfg=argv[1:], n=n):
                    self.setUp()
                    self.host.legacy_host(cfg)
                    hit = self.run_killed(argv, n)
                    self.assert_startable()
                    if not hit:
                        break
                    hits += 1
                    rc, out = self.main(*argv)
                    self.assertEqual(rc, 0, (n, out))
                    self.assert_moved(n)
            self.assertGreaterEqual(hits, 5)

    def test_every_rollback_kill_point(self):
        for cfg, argv in self.CASES:
            for n in range(1, 20):
                with self.subTest(cfg=argv[1:], n=n):
                    self.setUp()
                    self.host.legacy_host(cfg)
                    hit = self.run_killed(argv, n, healthy=False)
                    self.assert_startable()
                    if not hit:
                        self.assertEqual(self.host.pair(), (LEGACY_UNIT, cfg))   # all the way back
                        break
                    self.host.healthy = True
                    rc, out = self.main(*argv)
                    self.assertEqual(rc, 0, (n, out))
                    self.assert_moved(n)

    def test_the_recovery_run_killed_too(self):
        for cfg, argv in self.CASES:
            for first in range(1, 6):
                for second in range(1, 6):
                    with self.subTest(cfg=argv[1:], first=first, second=second):
                        self.setUp()
                        self.host.legacy_host(cfg)
                        self.run_killed(argv, first)
                        self.assert_startable()
                        self.run_killed(argv, second)
                        self.assert_startable()
                        rc, out = self.main(*argv)
                        self.assertEqual(rc, 0, out)
                        self.assert_moved((first, second))

    def test_a_move_cut_after_its_unit_write_is_checked_then_kept(self):
        """Killed after the unit write (or the daemon-reload): the old
        root-run ntfy still runs, so the files alone say nothing. The next
        run reloads systemd, restarts ntfy on the new pair and checks that it
        answers — kept, so there is nothing left to move."""
        for step in ("unit", "daemon-reload"):
            with self.subTest(step):
                self.setUp()
                self.host.legacy_host()
                real_w, real_s = self.h._atomic_write, self.h._systemctl

                def w(path, *a, **k):
                    real_w(path, *a, **k)
                    if step == "unit" and path == self.h.NTFY_UNIT:
                        raise Killed()

                def sc(*a, **k):
                    r = real_s(*a, **k)
                    if step == a[0]:
                        raise Killed()
                    return r
                self.h._atomic_write, self.h._systemctl = w, sc
                with contextlib.suppress(Killed):
                    self.main("ntfy-migrate")
                self.h._atomic_write, self.h._systemctl = real_w, real_s
                self.assertEqual(self.main("version")[1]["ntfy_unit"], "current")   # on disk only
                self.host.calls.clear()
                rc, out = self.main("ntfy-migrate")
                self.assertEqual((rc, out), (0, {"ok": True, "migrated": False, "already": True}))
                self.assertEqual(self.host.calls[0], ["systemctl", "daemon-reload"])
                restarts = [i for i, c in enumerate(self.host.calls) if c == ["systemctl", "restart", "ntfy"]]
                self.assertEqual(len(restarts), 1, self.host.calls)       # the new pair, once
                self.assertIn(["systemctl", "show", "-p", "ActiveState", "-p", "NRestarts", "ntfy"],
                              self.host.calls[restarts[0]:])
                self.assert_moved(step)

    def test_an_interrupted_health_wait_ends_rolled_back(self):
        """Ctrl-C while an unhealthy new setup is being waited on leaves it
        live; the next run puts the old pair back, tries again, and — still
        unhealthy — ends with the old pair (not "nothing to move")."""
        self.host.legacy_host()
        self.host.healthy = False
        n = {"k": 0}

        def sleep(s):
            n["k"] += 1
            if n["k"] == 3:
                raise KeyboardInterrupt()
            self.host.sleep(s)
        self.h.SLEEP = sleep
        with self.assertRaises(KeyboardInterrupt):
            self.main("ntfy-migrate")
        self.h.SLEEP = self.host.sleep
        self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
        rc, out = self.main("ntfy-migrate")
        self.assertEqual(rc, 1, out)
        self.assertIn("put back", out["message"])
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
        self.assertIn("ntfy", self.host.active)

    def test_a_journal_that_isnt_roots_own_is_never_used(self):
        self.host.legacy_host()
        j = self.f(self.h.NTFY_JOURNAL)
        j.write_text('{"unit": null, "cfg": null, "running": false}')
        j.chmod(0o644)                                     # not root's alone
        before = self.host.snapshot()
        rc, out = self.main("ntfy-migrate")
        self.assertEqual(rc, 1, out)
        self.assertIn("isn't root's own file", out["message"])
        self.assertEqual(self.host.snapshot(), before)

    def test_every_kill_point_of_a_fresh_install(self):
        """Then the same click again, or what the app offers next: with all
        four files there (aerodrome_managed) that is Upgrade — ntfy-install
        latest, without the settings."""
        args = ["ntfy-install", "latest", *NTFY_ARGS]
        for n, then in ((n, then) for n in range(1, 15) for then in ("same", "offered")):
            self.setUp()
            hit = self.run_killed(args, n)
            if not hit:
                break
            if self.f("/etc/systemd/system/ntfy.service").exists():
                self.assert_startable()
            files = all(self.f(p).exists() for p in (
                "/usr/local/bin/ntfy", "/etc/ntfy/server.yml", "/etc/systemd/system/ntfy.service",
                "/var/lib/ntfy/aerodrome-installed"))
            rc, out = self.main(*(args[:2] if then == "offered" and files else args))
            self.assertEqual(rc, 0, (n, then, out))
            self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
            self.assertTrue(self.f("/var/lib/ntfy/aerodrome-installed").exists())
            self.assertIn("ntfy", self.host.enabled, n)      # and running, not just on disk
            self.assertIn("ntfy", self.host.active, n)
            self.assert_runs_the_unit_on_disk(n)
            self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists(), n)

    def test_every_kill_point_of_uninstall_purge(self):
        for n in range(1, 20):
            self.setUp()
            self.host.legacy_host()
            self.f("/var/lib/ntfy/cache.db").write_bytes(b"db")
            self.f("/var/lib/private/aerodrome-ntfy").mkdir()
            self.f("/var/lib/private/aerodrome-ntfy/cache.db").write_bytes(b"db")
            h, count, real = self.h, {"n": 0}, self.h._remove_in

            def rm(*a, **k):
                r = real(*a, **k)
                count["n"] += 1
                if count["n"] == n:
                    raise Killed()
                return r
            h._remove_in = rm
            with contextlib.suppress(Killed):
                self.main("ntfy-uninstall", "--purge")
            h._remove_in = real
            if count["n"] < n:
                break
            self.assertIn(self.h._ntfy_state(), ("aerodrome_managed", "not_installed"))
            rc, out = self.main("ntfy-uninstall", "--purge")
            self.assertEqual(rc, 0, (n, out))
            self.assertFalse(self.f("/var/lib/ntfy/cache.db").exists(), n)
            self.assertFalse(self.f("/var/lib/private/aerodrome-ntfy/cache.db").exists(), n)
            self.assertFalse(self.f("/etc/ntfy").exists(), n)


class CutShort(_Case):
    """A change cut short (killed, interrupted, stopped along with
    aerodrome.service) leaves its journal. The next ntfy action settles it
    first, the way the cut run would have ended: a complete new pair that
    ntfy answers on is kept; a pair still in between, or one ntfy doesn't
    answer on, goes back. Upgrade (ntfy-install latest, what the app sends)
    must neither undo a change that worked nor tear down a fresh install."""

    def cut_while_waiting(self, *argv, sleeps=2):
        """Run argv, killed at the health wait's Nth sleep (2: ntfy has
        answered once and the wait is in its settle window)."""
        n = {"k": 0}

        def sleep(s):
            n["k"] += 1
            if n["k"] == sleeps:
                raise Killed()
            self.host.sleep(s)
        self.h.SLEEP = sleep
        try:
            with contextlib.suppress(Killed):
                self.main(*argv)
        finally:
            self.h.SLEEP = self.host.sleep
        self.assertEqual(n["k"], sleeps, "the run never got that far")
        self.assertTrue(self.f(self.h.NTFY_JOURNAL).exists())

    def cut_after_write(self, path, *argv):
        real = self.h._atomic_write

        def w(p, *a, **k):
            real(p, *a, **k)
            if p == path:
                raise Killed()
        self.h._atomic_write = w
        try:
            with contextlib.suppress(Killed):
                self.main(*argv)
        finally:
            self.h._atomic_write = real
        self.assertTrue(self.f(self.h.NTFY_JOURNAL).exists())

    def test_a_tab_save_that_moved_it_cut_after_ntfy_answered_is_kept_by_upgrade(self):
        self.host.legacy_host()
        args = list(NTFY_ARGS)
        args[5] = "http://192.0.2.44:2586"
        self.cut_while_waiting("ntfy-config", *args)
        self.host.serve_release("2.12.0")
        rc, out = self.main("ntfy-install", "latest")                # the app's Upgrade
        self.assertEqual((rc, out["version"], out["restarted"]), (0, "2.12.0", True), out)
        unit, cfg = self.host.pair()
        self.assertEqual(unit, self.h.NTFY_UNIT_TEXT, "the move was undone: ntfy runs as root again")
        self.assertEqual(self.h.classify_server_yml(cfg)["base_url"], "http://192.0.2.44:2586")
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
        self.assertEqual(self.main("version")[1]["ntfy_unit"], "current")
        self.assertIn("ntfy", self.host.active)

    def test_a_fresh_install_cut_after_ntfy_answered_is_kept_by_upgrade(self):
        """The app then sees all four files (aerodrome_managed): it offers
        Upgrade, not Install."""
        self.cut_while_waiting("ntfy-install", "latest", *NTFY_ARGS)
        for p in ("/usr/local/bin/ntfy", "/etc/ntfy/server.yml", "/etc/systemd/system/ntfy.service",
                  "/var/lib/ntfy/aerodrome-installed"):
            self.assertTrue(self.f(p).exists(), p)
        self.host.serve_release("2.12.0")
        rc, out = self.main("ntfy-install", "latest")
        self.assertEqual((rc, out.get("version")), (0, "2.12.0"), out)
        unit, cfg = self.host.pair()
        self.assertEqual(unit, self.h.NTFY_UNIT_TEXT)
        self.assertEqual(self.h.classify_server_yml(cfg)["base_url"], "http://192.0.2.10:2586")
        self.assertEqual(self.f("/var/lib/ntfy/aerodrome-installed").read_text(), "2.12.0\n")
        self.assertIn("ntfy", self.host.enabled)
        self.assertIn("ntfy", self.host.active)
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())

    def test_a_cut_change_ntfy_doesnt_answer_on_is_put_back_before_upgrade(self):
        """…so the pinned-version refusal sees the older root-run unit
        again: cutting a move short never gets a pinned build run as root."""
        self.host.legacy_host()
        self.h.PROBE = lambda port, timeout=2.0: port != 2590     # someone else holds 2590
        args = list(NTFY_ARGS)
        args[1], args[5] = "2590", "http://192.0.2.10:2590"
        self.cut_while_waiting("ntfy-config", *args, sleeps=3)
        self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
        self.host.serve_release("1.0.0")
        self.host.serve_release("2.12.0")
        rc, out = self.main("ntfy-install", "1.0.0")
        self.assertEqual(rc, 3, out)
        self.assertIn("root-run", out["message"])
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
        self.assertEqual(self.host.fetches, [])
        rc, out = self.main("ntfy-install", "latest")
        self.assertEqual((rc, out["version"]), (0, "2.12.0"), out)
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))
        self.assertIn("ntfy", self.host.active)

    def test_an_install_cut_while_ntfy_doesnt_answer_keeps_its_files(self):
        """Nothing to go back to — a fresh install, or the repair the app
        offers (Install) for a stamped install whose server.yml went missing
        while ntfy kept running: the next Upgrade reports that ntfy isn't
        answering, as the cut install would have — never a torn-down server
        and a 'needs --port' refusal."""
        for case in ("fresh", "repair"):
            with self.subTest(case):
                self.setUp()
                if case == "repair":
                    self.host.legacy_host()
                    self.f("/etc/ntfy/server.yml").unlink()
                self.host.healthy = False
                self.cut_while_waiting("ntfy-install", "latest", *NTFY_ARGS, sleeps=3)
                rc, out = self.main("ntfy-install", "latest")
                self.assertEqual(rc, 1, out)
                self.assertIn("isn't answering", out["message"])
                self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
                self.assertIsNotNone(self.host.pair()[1])
                self.assertIn("ntfy", self.host.enabled)
                self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())

    def test_a_repair_cut_short_runs_ntfy_on_the_unit_it_wrote(self):
        """The repair (Install) of a stamped install whose server.yml went
        missing while ntfy kept running on the older root-run unit, cut after
        the unit write or the daemon-reload: settling it (here by ntfy-migrate,
        the documented recovery, which then has nothing to move) restarts
        ntfy on the sandboxed unit — not a start that leaves the root-run
        process running, and answering the health check, behind it."""
        for step in ("unit", "daemon-reload"):
            with self.subTest(step):
                self.setUp()
                self.host.legacy_host()
                self.f("/etc/ntfy/server.yml").unlink()
                real_w, real_s = self.h._atomic_write, self.h._systemctl

                def w(path, *a, **k):
                    real_w(path, *a, **k)
                    if step == "unit" and path == self.h.NTFY_UNIT:
                        raise Killed()

                def sc(*a, **k):
                    r = real_s(*a, **k)
                    if step == a[0]:
                        raise Killed()
                    return r
                self.h._atomic_write, self.h._systemctl = w, sc
                try:
                    with contextlib.suppress(Killed):
                        self.main("ntfy-install", "latest", *NTFY_ARGS)
                finally:
                    self.h._atomic_write, self.h._systemctl = real_w, real_s
                self.assertTrue(self.f(self.h.NTFY_JOURNAL).exists())
                self.assertEqual(self.host.running_def, LEGACY_UNIT)
                rc, out = self.main("ntfy-migrate")
                self.assertEqual((rc, out), (0, {"ok": True, "migrated": False, "already": True}))
                self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
                self.assertIn("ntfy", self.host.active)
                self.assertEqual(self.host.running_def, self.h.NTFY_UNIT_TEXT,
                                 "ntfy still runs from the root-run unit")

    def test_a_change_to_a_stopped_server_cut_short_is_kept_without_starting_it(self):
        self.host.legacy_host(running=False)
        self.cut_after_write(self.h.NTFY_UNIT, "ntfy-config", *NTFY_ARGS)
        self.host.calls.clear()
        rc, out = self.main("ntfy-install", "latest")
        self.assertEqual((rc, out["restarted"]), (0, False), out)
        self.assertEqual(self.host.pair()[0], self.h.NTFY_UNIT_TEXT)
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
        self.assertNotIn("ntfy", self.host.active)
        self.assertIn(["systemctl", "daemon-reload"], self.host.calls)
        self.assertNotIn(["systemctl", "restart", "ntfy"], self.host.calls)

    def test_a_tab_save_settles_a_cut_move_before_its_own_change(self):
        """Otherwise its own journal would replace the leftover one, and its
        rollback would put back the half-done pair (the root-run unit with
        the new config) instead of the one that last worked."""
        self.host.legacy_host()
        self.cut_after_write(self.h.NTFY_CFG, "ntfy-migrate")
        self.assertEqual(self.host.pair()[0], LEGACY_UNIT)
        self.assertNotEqual(self.host.pair()[1], LEGACY_CFG)
        self.h.PROBE = lambda port, timeout=2.0: port != 2590
        args = list(NTFY_ARGS)
        args[1] = "2590"
        rc, out = self.main("ntfy-config", *args)
        self.assertEqual(rc, 1, out)
        self.assertIn("put back", out["message"])
        self.assertEqual(self.host.pair(), (LEGACY_UNIT, LEGACY_CFG))
        self.assertFalse(self.f(self.h.NTFY_JOURNAL).exists())
        self.assertIn("ntfy", self.host.active)


class Uninstall(_Case):

    def test_purge_never_follows_planted_links(self):
        """/var/cache/ntfy/staging belonged to the service account (the older
        setup's download folder): anything in it may be a planted link."""
        self.host.legacy_host()
        outside = self.host.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("keep")
        staging = self.f("/var/cache/ntfy/staging/sub")
        staging.mkdir(parents=True)
        (staging / "a.bin").write_bytes(b"x")
        os.symlink(str(outside), str(staging.parent / "link-dir"))
        os.symlink(str(outside / "sentinel"), str(staging.parent / "link-file"))
        self.f("/var/lib/private/aerodrome-ntfy").mkdir()
        state = self.f("/var/lib/private/aerodrome-ntfy")
        (state / "cache.db").write_bytes(b"db")
        self.f("/var/lib/aerodrome-ntfy").symlink_to("private/aerodrome-ntfy")
        self.f("/etc/ntfy/server.yml.pre-v6").write_text("old")
        rc, out = self.main("ntfy-uninstall", "--purge")
        self.assertEqual(rc, 0, out)
        self.assertEqual((outside / "sentinel").read_text(), "keep")
        self.assertFalse(self.f("/var/cache/ntfy").exists())
        self.assertFalse(self.f("/etc/ntfy").exists(), "server.yml.pre-v6 must not survive")
        self.assertFalse(state.exists())
        self.assertFalse(os.path.lexists(self.f("/var/lib/aerodrome-ntfy")))
        self.assertNotIn("ntfy", self.host.active)
        for p in ("/usr/local/bin/ntfy", "/etc/systemd/system/ntfy.service",
                  "/var/lib/ntfy/aerodrome-installed"):
            self.assertFalse(self.f(p).exists(), p)

    def test_without_purge_the_messages_stay(self):
        self.host.legacy_host()
        self.f("/var/lib/ntfy/cache.db").write_bytes(b"db")
        self.f("/var/lib/private/aerodrome-ntfy").mkdir()
        self.f("/var/lib/private/aerodrome-ntfy/cache.db").write_bytes(b"db")
        rc, out = self.main("ntfy-uninstall")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.f("/var/lib/ntfy/cache.db").exists())
        self.assertTrue(self.f("/var/lib/private/aerodrome-ntfy/cache.db").exists())
        self.assertFalse(self.f("/var/lib/ntfy/aerodrome-installed").exists())
        self.assertEqual(self.h._ntfy_state(), "not_installed")

    def test_the_official_packages_cache_is_never_purged(self):
        self.host.legacy_host()
        self.f("/var/cache/ntfy").mkdir()
        self.f("/var/cache/ntfy/cache.db").write_bytes(b"vendor")
        self.assertEqual(self.main("ntfy-uninstall", "--purge")[0], 0)
        self.assertTrue(self.f("/var/cache/ntfy/cache.db").exists())

    def test_a_change_cut_short_leaves_nothing_behind(self):
        """The journal of a change cut short (root's own, in /etc/ntfy) goes
        with the server: nothing of it is left behind."""
        self.host.legacy_host()
        real = self.h._atomic_write

        def w(p, *a, **k):
            real(p, *a, **k)
            if p == self.h.NTFY_CFG:
                raise Killed()
        self.h._atomic_write = w
        with contextlib.suppress(Killed):
            self.main("ntfy-migrate")
        self.h._atomic_write = real
        self.assertTrue(self.f(self.h.NTFY_JOURNAL).exists())
        rc, out = self.main("ntfy-uninstall")
        self.assertEqual(rc, 0, out)
        self.assertIn(self.h.NTFY_JOURNAL, out["removed"])
        self.assertFalse(self.f("/etc/ntfy").exists())

    def test_leftovers_purge_without_an_install(self):
        self.f("/var/lib/ntfy/cache.db").write_bytes(b"db")
        rc, out = self.main("ntfy-uninstall", "--purge")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["removed"], ["/var/lib/ntfy/cache.db"])
        self.assertFalse(self.f("/var/lib/ntfy/cache.db").exists())


FEEDER = "/etc/systemd/system/aerodrome-synthetic-feeder.service"
LEGACY_FEEDER = """[Service]
Type=simple
User=svc-demo
Group=svc-demo
WorkingDirectory=/opt/example
ExecStart=/opt/example/venv/bin/python3 -m tools.synthetic_feeder.serve \\
    --host 127.0.0.1 \\
    --port {port} \\
    --visible 50 \\
    --seed 1903
"""


def feeder_unit_from_install_sh(port="8080"):
    """The FEEDEREOF heredoc of this tree's install.sh, expanded as bash would."""
    text = (REPO / "install.sh").read_text()
    body = re.search(r"<< FEEDEREOF\n(.*?)\nFEEDEREOF\n", text, re.S).group(1)
    for k, v in {"${USER}": "svc-demo", "${INSTALL_DIR}": "/opt/example",
                 "${FEEDER_PORT:-8080}": port, "${HOME_LAT}": "40.0",
                 "${HOME_LON}": "-75.0"}.items():
        body = body.replace(k, v)
    return body.replace("\\\\\n", "\\\n") + "\n"


class Feeder(_Case):

    def test_set_port_changes_only_the_port(self):
        for shape in (feeder_unit_from_install_sh, lambda p: LEGACY_FEEDER.format(port=p)):
            with self.subTest(shape=shape):
                self.setUp()
                self.f(FEEDER).write_text(shape("8080"))
                rc, out = self.main("feeder-set-port", "8088")
                self.assertEqual((rc, out), (0, {"ok": True, "port": 8088}))
                self.assertEqual(self.f(FEEDER).read_text(), shape("8088"))
                self.assertEqual(self.host.calls[-2:], [
                    ["systemctl", "daemon-reload"],
                    ["systemctl", "restart", "aerodrome-synthetic-feeder.service"]])

    def test_refusals(self):
        self.assertEqual(self.main("feeder-set-port", "8088")[0], 3)        # not installed
        self.f(FEEDER).write_text("ExecStart=x --port 1 --port 2\n")
        self.assertEqual(self.main("feeder-set-port", "8088")[0], 1)        # two --port
        self.assertEqual(self.f(FEEDER).read_text(), "ExecStart=x --port 1 --port 2\n")
        for bad in ("80", "8088\nUser=root", "-1", "1023"):
            self.assertEqual(self.main("feeder-set-port", bad)[0], 2, bad)
        self.f(FEEDER).unlink()
        real = self.host.root / "elsewhere.service"
        real.write_text(feeder_unit_from_install_sh())
        self.f(FEEDER).symlink_to(real)
        self.assertEqual(self.main("feeder-set-port", "8088")[0], 1)        # a symlink
        self.assertEqual(real.read_text(), feeder_unit_from_install_sh())
        self.f(FEEDER).unlink()
        self.f(FEEDER).write_text(feeder_unit_from_install_sh())
        self.h.EXPECTED_OWNER_UID = os.getuid() + 1                          # not root's
        self.assertEqual(self.main("feeder-set-port", "8088")[0], 1)

    def test_a_feeder_that_wont_restart_gets_its_old_port_back(self):
        self.f(FEEDER).write_text(feeder_unit_from_install_sh("8080"))
        self.host.fail_restart.add("aerodrome-synthetic-feeder.service")
        rc, out = self.main("feeder-set-port", "8088")
        self.assertEqual(rc, 1, out)
        self.assertIn("put back", out["message"])
        self.assertEqual(self.f(FEEDER).read_text(), feeder_unit_from_install_sh("8080"))

    def test_remove_is_idempotent(self):
        self.f(FEEDER).write_text(feeder_unit_from_install_sh())
        self.host.active.add("aerodrome-synthetic-feeder.service")
        self.assertEqual(self.main("feeder-remove"), (0, {"ok": True, "removed": True}))
        self.assertEqual(self.main("feeder-remove"), (0, {"ok": True, "removed": False}))
        self.assertIn(["systemctl", "disable", "--now", "aerodrome-synthetic-feeder.service"],
                      self.host.calls)
        self.assertNotIn("aerodrome-synthetic-feeder.service", self.host.active)


class Lock(_Case):

    def test_a_held_lock_is_busy_within_lock_wait(self):
        fd = os.open(str(self.f("/run/aerodrome-helper.lock")), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            rc, out = self.main("feeder-remove")
        finally:
            os.close(fd)
        self.assertEqual((rc, out.get("busy")), (3, True))
        self.assertEqual(self.host.calls, [])


class Static(unittest.TestCase):
    """What a reviewer would otherwise check by hand on every change."""

    SRC = HELPER_SRC.read_text(encoding="utf-8")

    def test_shebang_and_marker(self):
        lines = self.SRC.splitlines()
        self.assertEqual(lines[0], "#!/usr/bin/python3 -IS")
        m = re.fullmatch(r"# PRIV_VERSION = (\d+)", lines[1])
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), load_helper().PRIV_VERSION)

    def test_python310_stdlib_only_and_no_shell(self):
        tree = ast.parse(self.SRC, feature_version=(3, 10))
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(mods - set(sys.stdlib_module_names), set())
        self.assertNotIn("shell=True", self.SRC)
        self.assertNotIn("os.system", self.SRC)
        self.assertIsNone(re.search(r"rmtree\(", self.SRC))

    def test_the_only_environment_read_is_sudo_uid(self):
        self.assertEqual(re.findall(r'os\.environ\.get\("(\w+)"', self.SRC), ["SUDO_UID"])
        self.assertEqual(self.SRC.count("os.environ"), 1)

    def test_argparse_hardening_and_the_frozen_action_set(self):
        p = load_helper().build_parser()
        self.assertIsNone(p.fromfile_prefix_chars)
        self.assertFalse(p.allow_abbrev)
        subs = next(a for a in p._actions if a.__class__.__name__ == "_SubParsersAction")
        self.assertEqual(sorted(subs.choices), sorted([
            "version", "ntfy-install", "ntfy-config", "ntfy-uninstall", "ntfy-migrate",
            "feeder-set-port", "feeder-remove"]))
        for sp in subs.choices.values():
            self.assertFalse(sp.allow_abbrev)
            self.assertIsNone(sp.fromfile_prefix_chars)

    def test_the_shared_block_is_byte_identical(self):
        def block(text):
            a = text.index("# --- BEGIN shared ntfy-yml block")
            b = text.index("# --- END shared ntfy-yml block ---")
            return text[a:b]
        self.assertEqual(block(self.SRC), block((REPO / "ntfy_installer.py").read_text()))

    def test_the_unit_passes_systemd_analyze_verify(self):
        exe = shutil.which("systemd-analyze")
        if not exe:
            self.skipTest("no systemd-analyze")
        with tempfile.TemporaryDirectory() as d:
            u = Path(d) / "ntfy.service"
            u.write_text(load_helper().NTFY_UNIT_TEXT)
            r = subprocess.run([exe, "verify", "--man=no", str(u)], capture_output=True,
                               text=True, stdin=subprocess.DEVNULL)
        text = r.stdout + r.stderr
        self.assertNotIn("Unknown key", text)
        self.assertNotIn("Invalid", text)
        self.assertNotIn("Failed to parse", text)


if __name__ == "__main__":
    unittest.main()
