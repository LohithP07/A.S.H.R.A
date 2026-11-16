from __future__ import annotations

import unittest
from datetime import datetime
from email.utils import formataddr

from ashra.mailparse import (
    IGNORE,
    INFORMATIONAL,
    REPLY_NEEDED,
    classify,
    clean_body,
    extract_asks,
    normalise_subject,
    parse_due,
    parse_message,
    strip_quoted,
    strip_signature,
    thread_key,
)
from tests.mail_fakes import build_message


class ParsingTests(unittest.TestCase):
    def test_headers_body_and_thread_are_extracted(self) -> None:
        raw = build_message(
            subject="Re: Deck for Tuesday",
            body="Could you send the deck by Tuesday?\n",
            message_id="<m2@example.com>",
            references=("<m1@example.com>",),
        )
        parsed = parse_message(raw)

        self.assertEqual(parsed.subject, "Deck for Tuesday")
        self.assertEqual(parsed.from_addr, "sarah@example.com")
        self.assertEqual(parsed.from_name, "Sarah Patel")
        self.assertIn("owner@example.com", parsed.to_addrs)
        self.assertEqual(parsed.thread_id, "m1@example.com")
        self.assertIn("send the deck", parsed.body)
        self.assertIsNotNone(parsed.occurred_at)
        self.assertFalse(parsed.automated)

    def test_quoted_history_is_removed_before_anything_else_sees_it(self) -> None:
        body = (
            "Here is the updated plan.\n\n"
            "On Mon, 10 Nov 2026 at 09:00, Sarah <sarah@example.com> wrote:\n"
            "> the old plan\n"
        )
        self.assertEqual(strip_quoted(body), "Here is the updated plan.\n")

    def test_signature_block_is_removed(self) -> None:
        body = "Short answer: yes.\n\n-- \nSarah\nAcme Coffee\n"
        self.assertEqual(strip_signature(body).strip(), "Short answer: yes.")

    def test_clean_body_collapses_blank_lines_and_is_bounded(self) -> None:
        cleaned = clean_body("a\n\n\n\nb" + "x" * 100, limit=20)
        self.assertEqual(len(cleaned), 20)
        self.assertNotIn("\n\n\n", cleaned)

    def test_subject_prefixes_are_normalised(self) -> None:
        self.assertEqual(normalise_subject("RE: Fwd: [list] Pricing"), "Pricing")
        self.assertEqual(normalise_subject("pricing"), "pricing")

    def test_attachment_content_is_never_retained(self) -> None:
        parsed = parse_message(build_message(attachment="deck.pdf"))
        self.assertEqual(len(parsed.attachments), 1)
        meta = parsed.attachments[0]
        self.assertEqual(meta.filename, "deck.pdf")
        self.assertEqual(meta.content_type, "application/pdf")
        self.assertGreater(meta.size, 0)
        # Only metadata survives: no field carries the attachment bytes.
        self.assertNotIn("PDF-1.4", repr(parsed.as_dict()))

    def test_thread_falls_back_to_the_message_id(self) -> None:
        self.assertEqual(thread_key("<a@x>", (), ""), "a@x")
        self.assertEqual(thread_key("<a@x>", ("<b@x>",), "<c@x>"), "b@x")


class ClassificationTests(unittest.TestCase):
    def test_newsletter_is_ignored_by_rule(self) -> None:
        raw = build_message(
            subject="The weekly digest",
            body="News you can use. Unsubscribe here.",
            list_id="coffee.example.com",
        )
        result = classify(parse_message(raw))
        self.assertEqual(result.label, IGNORE)
        self.assertGreaterEqual(result.confidence, 0.8)
        self.assertEqual(result.reason, "automated headers")

    def test_receipt_is_ignored_by_rule(self) -> None:
        result = classify(
            parse_message(build_message(subject="Your receipt from Acme"))
        )
        self.assertEqual(result.label, IGNORE)
        self.assertIn("receipt", result.reason)

    def test_no_reply_sender_is_ignored(self) -> None:
        raw = build_message(sender=("Acme", "noreply@acme.example"), subject="Hello")
        self.assertEqual(classify(parse_message(raw)).label, IGNORE)

    def test_request_is_reply_needed(self) -> None:
        result = classify(
            parse_message(
                build_message(
                    subject="Deck",
                    body="Could you send the final deck by Tuesday?\n",
                )
            )
        )
        self.assertEqual(result.label, REPLY_NEEDED)
        self.assertIn("request", result.reason)

    def test_question_without_a_request_is_still_reply_needed(self) -> None:
        result = classify(parse_message(build_message(body="Is the roastery open?\n")))
        self.assertEqual(result.label, REPLY_NEEDED)

    def test_plain_note_is_informational(self) -> None:
        result = classify(
            parse_message(build_message(body="The beans arrived this morning.\n"))
        )
        self.assertEqual(result.label, INFORMATIONAL)

    def test_asks_are_short_and_deduplicated(self) -> None:
        asks = extract_asks(
            "Please send the invoice today. Also, could you confirm the total?\n"
            "Please send the invoice today."
        )
        self.assertTrue(1 <= len(asks) <= 3)
        self.assertTrue(all(len(ask.text) <= 160 for ask in asks))
        self.assertEqual(len({ask.text.lower() for ask in asks}), len(asks))

    def test_asks_carry_an_unambiguous_due_hint(self) -> None:
        asks = extract_asks("Could you send the deck by Friday?")
        self.assertEqual(len(asks), 1)
        self.assertIsNotNone(asks[0].due_at)


class DueDateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Monday 16 November 2026, 10:00 local.
        self.now = datetime(2026, 11, 16, 10, 0)

    def test_tomorrow(self) -> None:
        due, hint = parse_due("please reply tomorrow", now=self.now)
        self.assertEqual(hint, "tomorrow")
        self.assertEqual(datetime.fromtimestamp(due).day, 17)

    def test_weekday(self) -> None:
        due, hint = parse_due("by Friday", now=self.now)
        self.assertEqual(hint, "by friday")
        self.assertEqual(datetime.fromtimestamp(due).weekday(), 4)

    def test_day_and_month(self) -> None:
        due, _hint = parse_due("by 17 Nov", now=self.now)
        self.assertEqual(datetime.fromtimestamp(due).month, 11)
        self.assertEqual(datetime.fromtimestamp(due).day, 17)

    def test_ambiguous_text_is_not_guessed(self) -> None:
        due, hint = parse_due("sometime soon-ish", now=self.now)
        self.assertIsNone(due)
        self.assertEqual(hint, "")

    def test_end_of_day(self) -> None:
        due, _hint = parse_due("EOD please", now=self.now)
        self.assertEqual(datetime.fromtimestamp(due).hour, 18)


if __name__ == "__main__":
    unittest.main()
