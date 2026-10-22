"""The app names its own account after the uid it runs as — never after an
inherited $LOGNAME/$USER naming someone else (`sudo -iu`, `su` and friends
can leave one behind). The one place it names itself: ntfy_installer.install()
makes /var/cache/ntfy owned by it (the sudoers rule pins `install -d …
-o <service account>`, and the staged binary is written there without sudo).

No network, no sudo, nothing written: install_status, latest_version and
_sudo_run are stubbed, and the run stops at the first sudo call.
Account names are invented.
"""
import os
import pwd
import unittest
from unittest import mock

import ntfy_installer as ni

OTHER = "admin-b"


class NtfyCacheDirOwner(unittest.TestCase):

    def run_install_until_first_sudo(self, env):
        calls = []

        def fake_sudo(cmd, input_text=None):
            calls.append(list(cmd))
            raise RuntimeError("stub: stop at the first sudo call")

        status = {"state": "not_installed", "arch": "amd64"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ni, "install_status", return_value=status), \
                mock.patch.object(ni, "latest_version", return_value="9.9.9"), \
                mock.patch.object(ni, "_sudo_run", side_effect=fake_sudo):
            result = ni.install(topic="demo-topic")
        return result, calls

    def test_owner_is_the_running_account_not_an_inherited_name(self):
        me = pwd.getpwuid(os.geteuid()).pw_name
        self.assertNotEqual(me, OTHER)
        result, calls = self.run_install_until_first_sudo(
            {"LOGNAME": OTHER, "USER": OTHER, "LNAME": OTHER, "USERNAME": OTHER})
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [["install", "-d", "-m", "0755", "-o", me, "-g", me,
                                  str(ni.CACHE_DIR)]])

    def test_falls_back_to_the_login_name_without_a_passwd_entry(self):
        # Guard: a uid the system can't look up (directory offline) keeps the
        # old login-name answer instead of an unhandled KeyError.
        with mock.patch("pwd.getpwuid", side_effect=KeyError("uid not found")):
            result, calls = self.run_install_until_first_sudo({"LOGNAME": "svc-a"})
        self.assertFalse(result["ok"])
        self.assertEqual(calls[0][4:8], ["-o", "svc-a", "-g", "svc-a"])


if __name__ == "__main__":
    unittest.main()
