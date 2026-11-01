"""The demo feeder's port recovery (collector._attempt_demo_port_recovery,
v3.4.150): config.yaml is staged, the system helper moves the feeder
(feeder-set-port: the unit's --port, daemon-reload, restart), and only then
is config.yaml committed — all under config_io.CONFIG_LOCK. The app writes
no unit itself.

The real helper runs on a fake root (test_aerodrome_helper's harness); the
listening ports are a set the test controls. No root, no network. Values
are invented.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collector
import config_io
import privhelper
from test_aerodrome_helper import HelperHost, HelperSudo, feeder_unit_from_install_sh

REPO = Path(__file__).resolve().parent
FEEDER = "/etc/systemd/system/aerodrome-synthetic-feeder.service"


def unit_port(text):
    import re
    return int(re.search(r"--port[ \t]+(\d+)", text).group(1))


class FeederPortRecovery(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aerodrome-recovery-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = HelperHost(self.tmp / "root")
        self.unit = self.host.f(FEEDER)
        self.unit.write_text(feeder_unit_from_install_sh("8080"))
        self.sudo = HelperSudo(self.host, privhelper.HELPER)
        inst = self.tmp / "install"
        inst.mkdir()
        self.cfgp = inst / "config.yaml"
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text())
        cfg["demo"]["enabled"] = True
        cfg["receiver"]["ip"], cfg["receiver"]["port"] = "127.0.0.1", 8080
        cfg["data"]["config_path"] = str(self.cfgp)
        self.cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.cfg = cfg
        self.busy = {8080}                         # something else took the feeder's port
        for p in (mock.patch.object(collector, "_FEEDER_UNIT_PATH", str(self.unit)),
                  mock.patch.object(collector, "_port_in_use", side_effect=lambda p: p in self.busy),
                  mock.patch("subprocess.run", side_effect=self.sudo)):
            p.start()
            self.addCleanup(p.stop)

    def recover(self):
        collector._demo_recovery_attempted = False
        self.addCleanup(setattr, collector, "_demo_recovery_attempted", False)
        return collector._attempt_demo_port_recovery(self.cfg)

    def config_port(self):
        return yaml.safe_load(self.cfgp.read_text())["receiver"]["port"]

    def test_the_helper_moves_the_feeder_between_stage_and_commit(self):
        seen = {}
        real = self.sudo.__call__

        def during_helper(cmd, *a, **kw):
            seen["config_port"] = self.config_port()
            seen["lock_held"] = not config_io.CONFIG_LOCK.acquire(blocking=False) or (
                config_io.CONFIG_LOCK.release() or False)
            return real(cmd, *a, **kw)
        with mock.patch("subprocess.run", side_effect=during_helper):
            port = self.recover()
        self.assertEqual(port, 8088)
        self.assertEqual(self.sudo.helper_calls(), [["feeder-set-port", "8088"]])
        self.assertEqual(seen["config_port"], 8080)            # not yet committed
        self.assertEqual(self.config_port(), 8088)             # committed after
        self.assertEqual(unit_port(self.unit.read_text()), 8088)
        self.assertEqual(self.cfg["receiver"]["port"], 8088)   # the running config too
        self.assertIn(["systemctl", "restart", "aerodrome-synthetic-feeder.service"], self.host.calls)
        self.assertFalse([c for c in self.sudo.calls if "tee" in c])
        self.assertEqual(sorted(p.name for p in self.cfgp.parent.iterdir()), ["config.yaml"])

    def test_the_lock_is_held_while_the_feeder_moves(self):
        held = []
        real = self.sudo.__call__

        def probe(cmd, *a, **kw):
            import threading
            got = []
            t = threading.Thread(target=lambda: got.append(config_io.CONFIG_LOCK.acquire(timeout=0.05)
                                                           and (config_io.CONFIG_LOCK.release() or True)))
            t.start()
            t.join()
            held.append(not got[0])
            return real(cmd, *a, **kw)
        with mock.patch("subprocess.run", side_effect=probe):
            self.assertEqual(self.recover(), 8088)
        self.assertEqual(held, [True])

    def test_a_helper_failure_leaves_config_yaml_alone(self):
        before = self.cfgp.read_bytes()
        for setup in ("unavailable", "refused"):
            with self.subTest(setup):
                self.sudo.unavailable = setup == "unavailable"
                if setup == "refused":
                    self.unit.write_text("ExecStart=x --port 1 --port 2\n")   # two --port
                with self.assertLogs("adsb.collector", "ERROR") as logs:
                    self.assertIsNone(self.recover())
                self.assertEqual(self.cfgp.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in self.cfgp.parent.iterdir()), ["config.yaml"])
                self.assertEqual(self.cfg["receiver"]["port"], 8080)
                text = "\n".join(logs.output)
                if setup == "unavailable":
                    self.assertIn("recover-sudoers.sh", text)

    def test_the_guard_still_allows_one_attempt_per_process(self):
        collector._demo_recovery_attempted = False
        self.sudo.unavailable = True
        with self.assertLogs("adsb.collector", "ERROR"):
            self.assertIsNone(collector._attempt_demo_port_recovery(self.cfg))
        self.sudo.unavailable = False
        self.assertIsNone(collector._attempt_demo_port_recovery(self.cfg))   # not again
        self.assertEqual(self.sudo.helper_calls(), [["feeder-set-port", "8088"]])
        collector._demo_recovery_attempted = False

    def test_cut_short_after_the_move_is_put_right_by_the_next_run(self):
        """Stopped after the helper moved the feeder but before config.yaml
        was committed: the next recovery run (the next start) finds the
        feeder's new port busy too, moves it to the next free one, and
        config.yaml and the unit agree again."""
        with mock.patch.object(config_io, "commit", side_effect=RuntimeError("cut short")), \
                self.assertLogs("adsb.collector", "ERROR"):
            self.assertIsNone(self.recover())
        self.assertEqual((self.config_port(), unit_port(self.unit.read_text())), (8080, 8088))
        self.cfg = yaml.safe_load(self.cfgp.read_text())        # a fresh start reads the file
        self.busy = {8080, 8088}                                # the feeder now listens on 8088
        port = self.recover()
        self.assertEqual(port, 28080)
        self.assertEqual(self.config_port(), unit_port(self.unit.read_text()))


if __name__ == "__main__":
    unittest.main()
