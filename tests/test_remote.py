"""How magrid reaches the frontends: through the user's shared ssh
connection when there is one, through one of its own otherwise."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from magrid import remote
from magrid.config import Config

SHARED = {"controlmaster": "auto", "controlpath": "/home/u/.ssh/cm-abc", "controlpersist": "14400",
          "proxycommand": 'ssh g5k -W "$(basename %h .g5k):%p"', "hostname": "nancy.g5k"}  # fmt: skip
PLAIN = {"controlmaster": "false", "controlpersist": "no", "hostname": "nancy.g5k"}  # ssh -G omits unset options


class Sharing(unittest.TestCase):
    def test_shared_needs_auto_and_a_path(self):
        self.assertTrue(remote.shares_connections(SHARED))
        self.assertFalse(remote.shares_connections(PLAIN))
        self.assertFalse(remote.shares_connections({"controlmaster": "auto"}))
        self.assertFalse(remote.shares_connections({"controlmaster": "auto", "controlpath": "none"}))
        self.assertFalse(remote.shares_connections({"controlmaster": "yes", "controlpath": "/tmp/x"}))  # not reused automatically

    def test_argv_follows_the_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}), \
                mock.patch.object(remote, "local_site", return_value=None):
            with mock.patch.object(remote, "ssh_settings", return_value=SHARED) as settings:
                r = remote.Remote(Config())
                argv = r._argv("nancy")
                self.assertEqual(argv[-2:], ["nancy.g5k", "bash -s"])
                self.assertFalse([a for a in argv if a.startswith("Control")], argv)
                r._argv("nancy")
                self.assertEqual(settings.call_count, 1)  # read once per host
            with mock.patch.object(remote, "ssh_settings", return_value=PLAIN):
                argv = remote.Remote(Config())._argv("nancy")
                self.assertIn("ControlMaster=auto", argv)
                self.assertIn("ControlPersist=300", argv)
                self.assertTrue(any(a.startswith(f"ControlPath={tmp}/magrid/") for a in argv), argv)

    @unittest.skipUnless(shutil.which("ssh"), "no ssh here")
    def test_settings_are_read_locally(self):
        s = remote.ssh_settings("nosuchhost.invalid")  # ssh -G: no connection is made
        self.assertEqual(s.get("hostname"), "nosuchhost.invalid")
        self.assertIn("controlmaster", s)


class Gateway(unittest.TestCase):
    def test_forwarding_only_is_fine(self):
        good, text, detail = remote.gateway_check(SHARED, "nancy.g5k")
        self.assertTrue(good)
        self.assertIn("-W", detail)
        good, text, _ = remote.gateway_check({"proxyjump": "access.grid5000.fr"}, "nancy.g5k")
        self.assertTrue(good)
        self.assertIn("ProxyJump", text)

    def test_a_command_on_the_gateway_is_refused(self):
        nc = {"proxycommand": "ssh g5k \"nc -q 0 `basename %h .g5k` %p\""}
        good, text, detail = remote.gateway_check(nc, "nancy.g5k")
        self.assertIs(good, False)
        self.assertIn("2026-10-29", detail)
        self.assertIn("-W", detail)

    def test_unknown_setups_are_only_reported(self):
        self.assertIsNone(remote.gateway_check(PLAIN, "nancy.g5k")[0])  # direct: on a Grid'5000 machine
        self.assertIsNone(remote.gateway_check({"proxycommand": "nc -X connect -x proxy:3128 %h %p"}, "nancy.g5k")[0])


if __name__ == "__main__":
    unittest.main()
