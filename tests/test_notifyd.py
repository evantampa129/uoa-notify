"""Tests for the notification daemon and the fallback chain around it.

The daemon exists because a notification outlives the process that posted it.
These tests pin the two properties that make that true: the request path
degrades instead of raising when no daemon is listening, and the daemon's own
contract with the notification server is the one that makes a body click
reach us.
"""

import os
import unittest

from context import U

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin")


class TestDaemonRequests(unittest.TestCase):
    """Nothing here may raise; a missing daemon is a normal state."""

    def setUp(self):
        self._saved = os.environ.get("XDG_RUNTIME_DIR")
        # Point at a directory with no socket in it, so the "not running"
        # branch is what actually gets exercised.
        os.environ["XDG_RUNTIME_DIR"] = "/nonexistent-runtime-dir"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._saved

    def test_socket_path_is_under_the_runtime_directory(self):
        self.assertTrue(U.notifyd_socket().startswith("/nonexistent-runtime-dir"))
        self.assertTrue(U.notifyd_socket().endswith("notifyd.sock"))

    def test_request_returns_none_when_no_socket_exists(self):
        self.assertIsNone(U.notifyd_request({"command": "ping"}))

    def test_request_survives_a_socket_that_is_not_a_socket(self):
        """A stale regular file at the socket path must not raise."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_RUNTIME_DIR"] = tmp
            os.makedirs(os.path.join(tmp, "uoa-notify"))
            with open(U.notifyd_socket(), "w", encoding="utf-8") as fh:
                fh.write("not a socket")
            self.assertIsNone(U.notifyd_request({"command": "ping"}))


class TestDaemonContract(unittest.TestCase):
    """Source-level checks on the parts that are awkward to exercise live."""

    def setUp(self):
        with open(os.path.join(BIN, "uoa-notifyd.py"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_registers_the_default_action(self):
        """`default` is what a click on the notification body invokes."""
        self.assertIn('"default", "Open"', self.text)

    def test_notifications_do_not_expire_on_their_own(self):
        """The daemon stays reachable, so there is no reason to expire.

        The old design had to bound the lifetime because a process was
        blocked on it; nothing is blocked now.
        """
        self.assertIn("actions, hints, 0)", self.text)

    def test_replaces_rather_than_stacks_by_tag(self):
        self.assertIn("self.by_tag.get(tag, 0)", self.text)

    def test_click_opens_before_it_records(self):
        """The browser must not wait on the Gmail round trip."""
        activate = self.text[self.text.index("def _activate"):]
        self.assertLess(activate.index("self._open"),
                        activate.index("self._mark_read"))

    def test_read_is_recorded_out_of_process(self):
        """A network call on the main loop would stall every notification."""
        mark = self.text[self.text.index("def _mark_read"):]
        self.assertIn("start_new_session=True", mark)


if __name__ == "__main__":
    unittest.main()
