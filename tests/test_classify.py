"""Tests for the Greek deadline classifier.

Every bug this project has actually shipped lived in these functions, so they
are the ones worth pinning down. Two are covered directly:

  * "εργασία" (assignment) matched inside "επεξεργασία" (Edit Profile), which
    turned a routine helpdesk mail into a red URGENT alert.
  * "σταθμό εργασίας" (workstation) failed to be blanked when the mail was
    hard-wrapped and the phrase straddled a line break.

All of this is pure text handling: no network, no IMAP, no state on disk.

Dates are always computed relative to ``date.today()``. A hardcoded date would
pass today and start failing once it drifted outside the classifier's window.
"""

import unittest
from datetime import date, timedelta

from context import U, default_config


def in_days(n):
    """A d/m/YYYY string exactly ``n`` days from today."""
    return (date.today() + timedelta(days=n)).strftime("%-d/%-m/%Y")


class TestNormalise(unittest.TestCase):
    """Accents and case must not affect matching."""

    def test_accents_and_case_are_folded(self):
        self.assertEqual(U.normalize("ΠΡΟΘΕΣΜΙΑ"), U.normalize("προθεσμία"))

    def test_normalise_handles_none(self):
        self.assertEqual(U.normalize(None), "")


class TestKeywordMatching(unittest.TestCase):
    """`kw_hits` anchors at a word start but allows Greek inflection."""

    def test_inflected_forms_still_match(self):
        """Every everyday form of "deadline" must register as one.

        There is no trailing word boundary, so a suffixed form such as
        "προθεσμίας" is reached by the singular keyword. The plural is not:
        it shifts the stem vowel (προθεσμι-α -> προθεσμι-ες), which is why
        "προθεσμιες" is carried in the keyword list in its own right.
        """
        for word in ("προθεσμία", "προθεσμίας", "προθεσμίες"):
            with self.subTest(word=word):
                self.assertTrue(
                    U.kw_hits(U.normalize(word), U.DEADLINE_KEYWORDS),
                    f"{word!r} was not recognised as deadline language")

    def test_keyword_inside_a_longer_word_does_not_match(self):
        # The regression: "επεξεργασία" contains "εργασία" but is not one.
        hits = U.kw_hits(U.normalize("Επεξεργασία προφίλ"), U.DEADLINE_KEYWORDS)
        self.assertNotIn("εργασια", hits)

    def test_word_start_after_punctuation_still_matches(self):
        hits = U.kw_hits(U.normalize("(εργασία 3)"), U.DEADLINE_KEYWORDS)
        self.assertIn("εργασια", hits)

    def test_kw_present_agrees_with_kw_hits(self):
        text = U.normalize("καταληκτική ημερομηνία")
        self.assertEqual(bool(U.kw_hits(text, U.DEADLINE_KEYWORDS)),
                         U.kw_present(text, U.DEADLINE_KEYWORDS))


class TestNoiseStripping(unittest.TestCase):
    """Noise phrases are blanked before matching, without moving offsets."""

    def test_workstation_phrase_is_blanked(self):
        out = U.strip_noise(U.normalize("από τον σταθμό εργασίας σας"))
        self.assertNotIn("εργασια", out)

    def test_offsets_are_preserved(self):
        # Blanking replaces characters with spaces rather than deleting them,
        # so date positions found later still line up with the original text.
        src = U.normalize("σταθμό εργασίας")
        self.assertEqual(len(U.strip_noise(src)), len(src))

    def test_phrase_split_across_a_line_break_is_still_blanked(self):
        # Mail arrives hard-wrapped; the phrase regularly straddles a newline.
        out = U.strip_noise(U.normalize("από τον σταθμό\nεργασίας σας"))
        self.assertNotIn("εργασια", out)


class TestClassify(unittest.TestCase):
    """End-to-end classification of realistic messages."""

    def setUp(self):
        self.cfg = default_config()

    def test_helpdesk_notice_is_not_a_deadline(self):
        # Shape of the NOC mail that originally produced a false URGENT.
        item = U.classify(
            "Ενημέρωση λογαριασμού",
            "Η επεξεργασία του προφίλ σας γίνεται από τον σταθμό\n"
            "εργασίας σας μέσω του NOC.",
            cfg=self.cfg)
        self.assertEqual(item["bucket"], "info")
        self.assertIsNone(item["due_date"])
        self.assertEqual(U.category_for(item), "info")

    def test_eclass_deadline_is_parsed_with_its_time(self):
        due = date.today() + timedelta(days=5)
        item = U.classify(
            "Δομές Δεδομένων: Εργασία 3",
            f"Καταληκτική ημερομηνία υποβολής: {in_days(5)}, 13:00",
            cfg=self.cfg)
        self.assertEqual(item["due_date"], due.isoformat())
        self.assertEqual(item["due_time"], "13:00")
        self.assertEqual(item["days_left"], 5)

    def test_bucket_boundaries(self):
        # urgent_days = 3, week_days = 7 in the defaults.
        for days, expected in ((2, "urgent"), (3, "urgent"),
                               (5, "week"), (7, "week"), (20, "info")):
            with self.subTest(days=days):
                item = U.classify("Εξέταση",
                                  f"Η εξέταση θα διεξαχθεί {in_days(days)}",
                                  cfg=self.cfg)
                self.assertEqual(item["due_date"],
                                 (date.today() + timedelta(days=days)).isoformat())
                self.assertEqual(item["bucket"], expected)

    def test_deadline_language_without_a_date_still_flags(self):
        # A hard keyword with no parseable date must not be silently dropped.
        item = U.classify("Προθεσμία", "Η προθεσμία λήγει σύντομα.", cfg=self.cfg)
        self.assertIsNone(item["due_date"])
        self.assertEqual(item["bucket"], "week")

    def test_message_own_timestamp_can_be_excluded(self):
        # eClass mail carries "Ημερομηνία: <sent date>" in its header; passing
        # it as excluded stops it being read as the due date.
        sent = date.today()
        item = U.classify(
            "Ανακοίνωση",
            f"Ημερομηνία: {sent.strftime('%-d/%-m/%Y')}\n"
            f"Προθεσμία υποβολής: {in_days(6)}",
            cfg=self.cfg, exclude_dates=(sent,))
        self.assertEqual(item["due_date"],
                         (date.today() + timedelta(days=6)).isoformat())

    def test_grade_message_is_categorised_as_a_grade(self):
        item = U.classify("Βαθμολογία 2ης προόδου",
                          "Αναρτήθηκαν οι βαθμοί.", cfg=self.cfg)
        self.assertTrue(item["grade_keywords"])
        self.assertEqual(U.category_for(item), "grade")

    def test_english_deadline_wording_is_matched(self):
        item = U.classify("Assignment 2",
                          f"Submission deadline: {in_days(4)}", cfg=self.cfg)
        self.assertEqual(item["bucket"], "week")
        self.assertIsNotNone(item["due_date"])


class TestDueString(unittest.TestCase):
    """`due_str` is what the user actually reads in an alert."""

    def test_empty_when_no_date(self):
        self.assertEqual(U.due_str({}), "")

    def test_relative_wording(self):
        base = {"due_date": "2026-09-01"}
        self.assertIn("(today!)", U.due_str({**base, "days_left": 0}))
        self.assertIn("(tomorrow)", U.due_str({**base, "days_left": 1}))
        self.assertIn("(in 4 days)", U.due_str({**base, "days_left": 4}))

    def test_time_is_appended_when_known(self):
        self.assertEqual(
            U.due_str({"due_date": "2026-09-01", "due_time": "13:00"}),
            "2026-09-01 13:00")


if __name__ == "__main__":
    unittest.main()
