"""The app never names an account or a group in a privileged call
(v3.4.150). Before, ntfy_installer.install() made /var/cache/ntfy owned by
the account it runs as (`install -d … -o <name> -g <name>`), which failed for
an account whose primary group isn't named after it (`useradd -g users`);
now the system helper creates everything as root, and nothing on the system
belongs to the service account. install.sh's own account handling (the
unit's account, its primary group for chown) is covered in
test_install_sh.py; the permission rules in test_sudoers_boundary.py.

No network, no sudo: subprocess.run is recorded and the helper runs on a
fake root. Account names are invented.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ntfy_installer as ni
import privhelper
from test_aerodrome_helper import HelperHost, HelperSudo

OTHER = "admin-b"


class NoAccountInPrivilegedCalls(unittest.TestCase):

    def test_install_names_no_account_or_group_whatever_the_environment(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        host = HelperHost(tmp / "root")
        sudo = HelperSudo(host, privhelper.HELPER)
        status = {"state": "not_installed", "arch": "linux_amd64"}
        env = {"LOGNAME": OTHER, "USER": OTHER, "LNAME": OTHER, "USERNAME": OTHER}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ni, "install_status", return_value=status), \
                mock.patch.object(ni, "_detect_lan_ip", return_value=("192.0.2.10", None)), \
                mock.patch("subprocess.run", side_effect=sudo):
            result = ni.install(topic="demo-topic")
        self.assertTrue(result["ok"], result)
        calls = [c for c in sudo.calls if c[:1] == ["sudo"]]
        self.assertEqual([c[3] for c in calls], ["ntfy-install"])
        for c in calls:
            for flag in ("-o", "-g", OTHER, "users"):
                self.assertNotIn(flag, c)
        self.assertFalse(host.f("/var/cache/ntfy").exists())     # no service-account folder
        src = Path(ni.__file__).read_text()
        self.assertNotIn("getpass", src)
        self.assertNotIn("pwd.getpwuid", src)


if __name__ == "__main__":
    unittest.main()
