from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from ashra.notes import Note
from ashra.retrieval import score_notes, tokenize


def make_note(
    note_id: int,
    title: str,
    text: str,
    *,
    group: str = "General",
    path: str | None = None,
    created: datetime | None = None,
) -> Note:
    return Note(
        id=note_id,
        label=title,
        title=title,
        group=group,
        path=path or f"{title.lower().replace(' ', '-')}.md",
        excerpt=text[:100],
        text=text,
        words=len(text.split()),
        created=created,
    )


class RetrievalTests(unittest.TestCase):
    def test_stopwords_are_removed(self) -> None:
        tokens = tokenize("What is the pricing strategy?")
        self.assertIn("pricing", tokens)
        self.assertIn("strategy", tokens)
        self.assertNotIn("what", tokens)
        self.assertNotIn("the", tokens)

    def test_p_and_l_becomes_pl(self) -> None:
        self.assertIn("pl", tokenize("How did our P&L look?"))

    def test_title_matches_outrank_body_matches(self) -> None:
        titled = make_note(1, "Pricing Strategy", "A short note.")
        body = make_note(2, "General Notes", "pricing strategy pricing strategy")
        scored = score_notes([titled, body], "pricing strategy")
        self.assertEqual(scored[0].note.id, 1)
        self.assertGreater(scored[0].score, scored[1].score)

    def test_history_supports_follow_up_questions(self) -> None:
        note = make_note(1, "Wholesale Accounts", "Wholesale account details.")
        scored = score_notes(
            [note],
            "What about them?",
            history=["What about wholesale?"],
        )
        self.assertEqual(len(scored), 1)
        self.assertEqual(scored[0].note.id, 1)

    def test_recent_notes_get_a_recency_boost(self) -> None:
        now = datetime.now().astimezone()
        recent = make_note(
            1,
            "Pricing Update",
            "pricing strategy",
            created=now - timedelta(days=1),
        )
        old = make_note(
            2,
            "Pricing Archive",
            "pricing strategy",
            created=now - timedelta(days=365),
        )

        scored = score_notes([old, recent], "latest pricing strategy")

        self.assertEqual(scored[0].note.id, 1)
        self.assertGreater(scored[0].score, scored[1].score)


if __name__ == "__main__":
    unittest.main()
