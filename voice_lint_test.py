"""Tests for voice_lint_bridge. Run: .venv/bin/python -m unittest voice_lint_test"""
import unittest

from voice_lint_bridge import _lint

LINKEDIN_FOOTER = (
    "Community: britleydev.slack.com\n"
    "Repo: https://github.com/britleyhoff/claude-tools\n"
    "Book: https://books.britleyhoffconsulting.com\n"
    "Contact: britleyhoff@britleyhoffconsulting.com\n"
)


def found(text, kind="generic"):
    return _lint(text, kind)


def has(text, needle, kind="generic", sev=None):
    return any(needle in m and (sev is None or s == sev) for _, s, m in _lint(text, kind))


class VoiceLintTest(unittest.TestCase):
    def test_clean_prose(self):
        self.assertEqual(found("Plain sentence, with a comma. A well-known off-site tool."), [])

    def test_em_dash(self):
        self.assertTrue(has("It broke — badly.", "em dash"))

    def test_spaced_hyphen(self):
        self.assertTrue(has("It broke - badly.", "spacing dash"))
        self.assertTrue(has("It broke -- badly.", "spacing dash"))

    def test_spaced_en_dash(self):
        self.assertTrue(has("It broke – badly.", "en dash"))
        self.assertFalse(has("Pages 10–20.", "en dash"))

    def test_compound_words_ok(self):
        self.assertEqual(found("A well-known, off-site, 10-year, z/OS-based thing."), [])

    def test_bullets_ok(self):
        self.assertEqual(found("- one\n- two\n  - nested\n"), [])

    def test_table_separator_ok(self):
        self.assertEqual(found("| a | b |\n| --- | --- |\n"), [])

    def test_code_is_skipped(self):
        self.assertEqual(found("Run `ls - l` now.\n```\nfoo - bar — baz\n```\n"), [])

    def test_url_hyphens_skipped(self):
        self.assertEqual(found("See https://example.com/a-b-c today"), [])
        self.assertTrue(has("See https://example.com/a - b today", "spacing dash"))

    def test_line_numbers(self):
        r = found("fine\nfine\nbad — here\n")
        self.assertEqual(r[0][0], 3)

    def test_corporate_is_warning(self):
        r = found("We will leverage synergy.")
        self.assertTrue(r and all(s == "WARN" for _, s, _ in r))

    def test_linkedin_requires_links(self):
        for needle in ("Slack", "GitHub", "book link", "contact email"):
            self.assertTrue(has("Hello there.", needle, "linkedin"), needle)

    def test_linkedin_complete_is_clean(self):
        self.assertEqual(found("Real moment, plain words.\n\n" + LINKEDIN_FOOTER, "linkedin"), [])

    def test_linkedin_bans_consulting_site(self):
        self.assertTrue(has("Hi\nhttps://britleyhoffconsulting.com\n" + LINKEDIN_FOOTER, "retired", "linkedin"))
        self.assertTrue(has("Hi\nbritleyhoffconsulting.com\n" + LINKEDIN_FOOTER, "retired", "linkedin"))

    def test_book_and_email_are_not_the_retired_site(self):
        self.assertFalse(has("Hi\n" + LINKEDIN_FOOTER, "retired", "linkedin"))

    def test_linkedin_length(self):
        self.assertTrue(has("x" * 3000 + "\n" + LINKEDIN_FOOTER, "over LinkedIn", "linkedin"))

    def test_commit_rules(self):
        self.assertEqual(found("feat: add thing\n\nWhy.\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n", "commit"), [])
        self.assertTrue(has("add thing\n\nCo-Authored-By: x\n", "prefix", "commit"))
        self.assertTrue(has("fix: thing\n", "Co-Authored-By", "commit"))


if __name__ == "__main__":
    unittest.main()
