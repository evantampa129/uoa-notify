"""Tests for the subject tags that carry read state between devices.

A forwarded notification is matched back to its local ledger entry by the
``[XXX-MSG-xxxxxxxx]`` tag in its subject line. If tag generation ever drifts,
nothing raises: the sync job simply stops finding the message, the item is
never marked read, and it is re-notified forever. That silence is the reason
these properties are pinned down here.

Two properties matter:

  * **Determinism** - the same (source, message_id) must always produce the
    same tag, across runs, machines and Python versions.
  * **Round-tripping** - a tag written into a subject must be readable back
    out of it, including when the subject was truncated to fit a length limit.
"""

import unittest

from context import U


class TestMakeTag(unittest.TestCase):
    """Tag generation."""

    def test_is_deterministic(self):
        # Recomputed on every run rather than stored, so it must be a pure
        # function of its inputs.
        a = U.make_tag("eclass", "<abc123@eclass.uoa.gr>")
        b = U.make_tag("eclass", "<abc123@eclass.uoa.gr>")
        self.assertEqual(a, b)

    def test_differs_by_message_id(self):
        self.assertNotEqual(U.make_tag("eclass", "one"),
                            U.make_tag("eclass", "two"))

    def test_differs_by_source(self):
        # The same id arriving from two sources is two distinct items.
        self.assertNotEqual(U.make_tag("eclass", "same-id"),
                            U.make_tag("webmail", "same-id"))

    def test_source_prefix_is_used(self):
        for source, code in U.TAG_CODES.items():
            with self.subTest(source=source):
                self.assertTrue(U.make_tag(source, "x").startswith(code + "-MSG-"))

    def test_unknown_source_falls_back_to_gen(self):
        self.assertTrue(U.make_tag("nonesuch", "x").startswith("GEN-MSG-"))

    def test_shape_matches_the_reader_regex(self):
        # Generation and extraction must agree on the format, or sync breaks.
        tag = U.make_tag("webmail", "<id@uoa.gr>")
        self.assertEqual(U.tag_in(f"[{tag}]"), tag)

    def test_non_ascii_message_ids_are_handled(self):
        # Some ids carry the raw Greek subject; hashing must not raise.
        tag = U.make_tag("eclass", "Δομές Δεδομένων — Εργασία 3")
        self.assertTrue(U.TAG_RE.search(f"[{tag}]"))


class TestTagIn(unittest.TestCase):
    """Tag extraction from a subject line."""

    def test_extracts_from_a_realistic_subject(self):
        tag = U.make_tag("webmail", "<id@uoa.gr>")
        subject = f"🟡 UoA DEADLINE: Εργασία 3 — Δομές Δεδομένων [{tag}]"
        self.assertEqual(U.tag_in(subject), tag)

    def test_returns_empty_when_absent(self):
        self.assertEqual(U.tag_in("Ordinary subject with no tag"), "")

    def test_returns_empty_for_none(self):
        self.assertEqual(U.tag_in(None), "")

    def test_ignores_bracketed_text_that_is_not_a_tag(self):
        self.assertEqual(U.tag_in("[URGENT] Please read"), "")

    def test_ignores_an_unknown_source_code(self):
        # Only the five known prefixes are accepted.
        self.assertEqual(U.tag_in("[XXX-MSG-deadbeef]"), "")

    def test_rejects_a_wrong_length_digest(self):
        self.assertEqual(U.tag_in("[UOA-MSG-abc]"), "")

    def test_finds_the_tag_after_a_reply_prefix(self):
        # Forwards and replies prepend to the subject; the tag stays findable.
        tag = U.make_tag("gmail", "<id@gmail.com>")
        self.assertEqual(U.tag_in(f"Re: Fwd: something [{tag}]"), tag)


if __name__ == "__main__":
    unittest.main()
