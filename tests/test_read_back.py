"""Tests for what happens after a notification is clicked.

The chain is: the user activates the notification, notify-open.sh opens the
page and asks notify.py to mark the item read, notify.py records that locally
and pushes it to Gmail, and sync-read-status.py must then not undo it. Each
link has a way of failing quietly, so each is pinned here.
"""

import os
import sys
import tempfile
import unittest

from context import U

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import notify  # noqa: E402


class LedgerFixture(unittest.TestCase):
    """Point the ledger at a scratch directory so no real state is touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_state = U.STATE_DIR
        U.STATE_DIR = self._tmp.name

    def tearDown(self):
        U.STATE_DIR = self._saved_state
        self._tmp.cleanup()

    def make_entry(self, source="eclass", message_id="42", **extra):
        state = U.ledger_load(source)
        entry = U.ledger_record(state, message_id, source=source,
                                subject="Εργασία 3", category="deadline",
                                tag=U.make_tag(source, message_id),
                                forwarded=True, **extra)
        U.ledger_save(source, state)
        return entry


class TestMarkRead(LedgerFixture):

    def test_marks_the_entry_read_by_tag(self):
        """notify-open.sh only has the tag, so the tag must be enough."""
        entry = self.make_entry()
        self.assertTrue(notify.mark_read(tag=entry["tag"]))
        after = U.ledger_load("eclass")["items"]["42"]
        self.assertTrue(after["read"])
        self.assertIn("opened_at", after)

    def test_marks_the_entry_read_by_source_and_id(self):
        self.make_entry(source="webmail", message_id="7")
        self.assertTrue(notify.mark_read(source="webmail", message_id="7"))
        self.assertTrue(U.ledger_load("webmail")["items"]["7"]["read"])

    def test_unknown_tag_is_not_fatal(self):
        """A stale notification from a pruned item must not raise."""
        self.assertFalse(notify.mark_read(tag="UOA-MSG-deadbeef"))

    def test_missing_identifiers_are_not_fatal(self):
        self.assertFalse(notify.mark_read())

    def test_dry_run_changes_nothing(self):
        entry = self.make_entry()
        self.assertTrue(notify.mark_read(tag=entry["tag"], dry_run=True))
        self.assertFalse(U.ledger_load("eclass")["items"]["42"]["read"])

    def test_push_is_flagged_pending_when_gmail_is_unreachable(self):
        """With no assistant CLI configured the push cannot happen.

        It must be recorded as owed rather than dropped, or the next sync
        would read Gmail's stale "unread" and undo the click.
        """
        self.make_entry(gmail_msg_id="abc123")
        saved = os.environ.pop("UOA_AGENT_CLI", None)
        try:
            notify.mark_read(source="eclass", message_id="42")
        finally:
            if saved is not None:
                os.environ["UOA_AGENT_CLI"] = saved
        after = U.ledger_load("eclass")["items"]["42"]
        self.assertTrue(after["read"])
        self.assertTrue(after["read_push_pending"])


class TestTagLookup(LedgerFixture):

    def test_finds_an_entry_across_sources(self):
        entry = self.make_entry(source="department", message_id="3160")
        source, _, found = U.ledger_find_by_tag(entry["tag"])
        self.assertEqual(source, "department")
        self.assertEqual(found["message_id"], "3160")

    def test_returns_none_for_an_unknown_tag(self):
        source, state, entry = U.ledger_find_by_tag("XXX-MSG-00000000")
        self.assertIsNone(source)
        self.assertIsNone(entry)


class TestNotifyOpenContract(unittest.TestCase):
    """The shell helper and the python hub have to agree on the interface."""

    def setUp(self):
        self.script = os.path.join(BIN, "notify-open.sh")
        with open(self.script, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_registers_the_default_action(self):
        """Clicking the notification *body* fires the action named "default".

        Without it the banner looks clickable and does nothing, which is the
        bug this file exists to prevent coming back.
        """
        self.assertIn("--action=default=", self.text)

    def test_handles_the_default_action_key(self):
        self.assertRegex(self.text, r"default\|open\|")

    def test_bounds_every_blocking_call(self):
        """GNOME Shell ignores --expire-time, so --wait never returns on its
        own; an unbounded helper pins the per-item lock forever."""
        self.assertIn('timeout "$WAIT_SECS"', self.text)
        # Mentioned in the comments, never passed as an argument.
        self.assertNotIn("--expire-time=", self.text)

    def test_invokes_the_read_back(self):
        self.assertIn("--mark-read", self.text)


if __name__ == "__main__":
    unittest.main()
