from __future__ import annotations

import unittest

from ashra.persona import small_talk_reply


class PersonaTests(unittest.TestCase):
    def test_identity_uses_the_project_name(self) -> None:
        reply = small_talk_reply("who are you")
        self.assertIsNotNone(reply)
        self.assertIn("A.S.H.R.A", reply or "")

    def test_name_prefix_does_not_block_a_greeting(self) -> None:
        reply = small_talk_reply("A.S.H.R.A, hello")
        self.assertIsNotNone(reply)
        self.assertIn("sir", (reply or "").lower())

    def test_question_after_a_greeting_is_not_small_talk(self) -> None:
        self.assertIsNone(small_talk_reply("Hello, what is our pricing strategy?"))


if __name__ == "__main__":
    unittest.main()
