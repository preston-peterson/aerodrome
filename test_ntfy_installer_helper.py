"""ntfy_installer's privileged calls (v3.4.150): every entry point that
changes the system makes exactly one kind of call — `sudo -n <helper> <sub>
…` — never tee, install, rm or systemctl through sudo.

subprocess.run is recorded; the helper runs for real on a fake root
(test_aerodrome_helper's harness). No root, no network. Values are invented.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import ntfy_installer
import privhelper
from test_aerodrome_helper import LEGACY_CFG, HelperHost, HelperSudo


class OnlyHelperCalls(unittest.TestCase):

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.host = HelperHost(tmp / "root")
        self.sudo = HelperSudo(self.host, privhelper.HELPER)
        fake = self.host.root
        self.state = "not_installed"
        for name, value in {
                "BINARY_PATH": fake / "usr/local/bin/ntfy",
                "CONFIG_DIR": fake / "etc/ntfy", "CONFIG_FILE": fake / "etc/ntfy/server.yml",
                "SYSTEMD_UNIT": fake / "etc/systemd/system/ntfy.service",
                "STAMP_FILE": fake / "var/lib/ntfy/aerodrome-installed",
                "STAMP_DIR": fake / "var/lib/ntfy",
                "VENDOR_PATHS": tuple(fake / p.lstrip("/") for p in ntfy_installer.NTFY_VENDOR_PATHS),
                "_systemd_service_status": lambda *a: {"active": False, "enabled": False},
                "_binary_version": lambda *a: None, "_detect_arch": lambda: "linux_amd64",
                "_detect_lan_ip": lambda: ("192.0.2.10", None),
                "latest_version": lambda: "2.12.0"}.items():
            p = mock.patch.object(ntfy_installer, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch("subprocess.run", side_effect=self.sudo),
                  mock.patch.object(requests, "get", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    def test_every_entry_point(self):
        self.assertTrue(ntfy_installer.install(port=2586, bind="0.0.0.0")["ok"])
        self.assertFalse(ntfy_installer.stale_data_present())
        r = ntfy_installer.update_config(base_url="http://192.0.2.20:2586")
        self.assertTrue(r["ok"], r)
        self.host.serve_release("2.12.0")
        self.assertTrue(ntfy_installer.upgrade()["ok"])
        self.assertTrue(ntfy_installer.uninstall(purge_data=True)["ok"])
        self.assertTrue(ntfy_installer.uninstall(purge_data=True)["ok"])    # leftovers: none
        self.assertTrue(ntfy_installer.stale_data_present() is False)
        sudo = [c for c in self.sudo.calls if c[:1] == ["sudo"]]
        self.assertTrue(sudo)
        for c in sudo:
            self.assertEqual(c[:3], ["sudo", "-n", str(privhelper.HELPER)], c)
        self.assertEqual([c[3] for c in sudo],
                         ["ntfy-install", "ntfy-config", "ntfy-install",
                          "ntfy-uninstall", "ntfy-uninstall", "version"])

    def test_a_partial_install_with_a_hand_added_setting_is_refused_unchanged(self):
        """The 'partial' state (stamp, a file missing) reaches install() only
        by a direct POST: the helper refuses rather than replace server.yml."""
        self.host.f("/var/lib/ntfy/aerodrome-installed").write_text("2.10.0\n")
        cfg = LEGACY_CFG + 'auth-file: "/srv/example/user.db"\n'
        self.host.f("/etc/ntfy/server.yml").write_text(cfg)
        self.assertEqual(ntfy_installer.install_status()["state"], "partial")
        r = ntfy_installer.install()
        self.assertEqual((r["ok"], r["code"], r["custom_keys"]), (False, "custom_keys", ["auth-file"]))
        self.assertEqual(self.host.f("/etc/ntfy/server.yml").read_text(), cfg)


    # --- v3.4.166 Repair: a partly installed ntfy is reinstalled over what's
    # there, keeping the settings server.yml still holds. ---

    def partial(self, binary=True, cfg=True, unit=True,
                yml=ntfy_installer.render_server_yml(2700, "127.0.0.1", "http://192.0.2.30:2700", False)):
        self.host.f("/var/lib/ntfy/aerodrome-installed").write_text("2.10.0\n")
        if binary:
            self.host.f("/usr/local/bin/ntfy").write_bytes(b"old ntfy")
        if cfg:
            self.host.f("/etc/ntfy/server.yml").write_text(yml)
        if unit:
            self.host.f("/etc/systemd/system/ntfy.service").write_text(ntfy_installer.NTFY_UNIT_TEXT)
        self.assertEqual(ntfy_installer.install_status()["state"], "partial")
        return yml

    def test_repair_puts_back_the_program_and_keeps_the_settings(self):
        yml = self.partial(binary=False)
        r = ntfy_installer.install(port=2586, bind="0.0.0.0", topic="example-topic")   # the form's defaults
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["repaired"])
        self.assertEqual(r["topic"], "example-topic")
        self.assertIn("repaired", r["message"])
        self.assertTrue(self.host.f("/usr/local/bin/ntfy").exists())
        self.assertEqual(self.host.f("/etc/ntfy/server.yml").read_text(), yml)
        self.assertEqual(ntfy_installer.install_status()["state"], "aerodrome_managed")

    def test_repair_of_a_missing_service_keeps_the_settings(self):
        self.partial(unit=False)
        r = ntfy_installer.install(port=2586, bind="0.0.0.0")
        self.assertTrue(r["ok"], r)
        cur = ntfy_installer.current_config()
        self.assertEqual((cur["port"], cur["bind"], cur["base_url"], cur["upstream"]),
                         (2700, "127.0.0.1", "http://192.0.2.30:2700", False))
        self.assertEqual(ntfy_installer.install_status()["state"], "aerodrome_managed")

    def test_repair_of_a_missing_settings_file_uses_the_form(self):
        self.partial(cfg=False)
        r = ntfy_installer.install(port=2590, bind="127.0.0.1")
        self.assertTrue(r["ok"], r)
        cur = ntfy_installer.current_config()
        self.assertEqual((cur["port"], cur["bind"]), (2590, "127.0.0.1"))
        self.assertEqual(ntfy_installer.install_status()["state"], "aerodrome_managed")

if __name__ == "__main__":
    unittest.main()
