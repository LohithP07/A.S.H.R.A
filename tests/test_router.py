from __future__ import annotations

import unittest

from ashra.router import (
    LANE_CLOUD,
    LANE_LOCAL,
    LANE_REFUSAL,
    TASK_RULES,
    LaneRouter,
    Route,
    task_lane,
)


class TableTests(unittest.TestCase):
    def test_every_prd_task_is_present(self) -> None:
        expected = {
            "email_classification",
            "email_extraction",
            "email_draft",
            "calendar_summary",
            "planning",
            "general_chat",
            "web_research",
        }
        self.assertTrue(expected <= set(TASK_RULES))

    def test_private_tasks_never_declare_a_fallback(self) -> None:
        for task in (
            "email_classification",
            "email_extraction",
            "email_draft",
            "calendar_summary",
            "planning",
        ):
            lane, fallback = task_lane(task)
            self.assertEqual(lane, LANE_LOCAL, task)
            self.assertIsNone(fallback, task)

    def test_unknown_task_defaults_to_general_chat(self) -> None:
        self.assertEqual(task_lane("something_new"), (LANE_CLOUD, LANE_LOCAL))


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = LaneRouter()

    def test_email_classification_is_local_even_when_public(self) -> None:
        route = self.router.decide("email_classification", "public")
        self.assertEqual(route.lane, LANE_LOCAL)
        self.assertIsNone(route.fallback)

    def test_general_chat_with_private_content_stays_local(self) -> None:
        route = self.router.decide("general_chat", "private")
        self.assertEqual(route.lane, LANE_LOCAL)
        self.assertIsNone(route.fallback)
        self.assertIn("FR-P2", route.reason)

    def test_sensitive_planning_is_local(self) -> None:
        route = self.router.decide("planning", "sensitive")
        self.assertEqual(route.lane, LANE_LOCAL)

    def test_secret_is_a_refusal(self) -> None:
        route = self.router.decide("general_chat", "secret")
        self.assertEqual(route.lane, LANE_REFUSAL)
        self.assertIsNone(route.fallback)
        self.assertIn("FR-P3", route.reason)

    def test_public_general_chat_uses_cloud_with_local_fallback(self) -> None:
        route = self.router.decide("general_chat", "public")
        self.assertEqual(route.lane, LANE_CLOUD)
        self.assertEqual(route.fallback, LANE_LOCAL)

    def test_internal_web_research_uses_cloud(self) -> None:
        self.assertEqual(
            self.router.decide("web_research", "internal").lane,
            LANE_CLOUD,
        )

    def test_cloud_can_be_narrowed_by_configuration(self) -> None:
        router = LaneRouter(allow_cloud_for=("public",))
        self.assertEqual(
            router.decide("general_chat", "internal").lane,
            LANE_LOCAL,
        )

    def test_extra_refusal_class_is_honoured(self) -> None:
        router = LaneRouter(refuse=("sensitive", "secret"))
        self.assertEqual(
            router.decide("calendar_summary", "sensitive").lane,
            LANE_REFUSAL,
        )


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = LaneRouter()

    def test_contact_detail_makes_a_chat_private(self) -> None:
        route = self.router.route(
            "general_chat",
            text="Email sarah@example.com about the deck.",
        )
        self.assertEqual(route.lane, LANE_LOCAL)
        self.assertEqual(route.sensitivity, "private")

    def test_a_credential_shaped_string_is_refused(self) -> None:
        route = self.router.route(
            "general_chat",
            text="Deploy with api_key=abcdefghijklmnop1234",
        )
        self.assertEqual(route.lane, LANE_REFUSAL)

    def test_declared_class_is_a_floor_not_a_ceiling(self) -> None:
        route = self.router.route(
            "general_chat",
            text="Salary is 120000 and the diagnosis is private.",
            declared="internal",
        )
        self.assertEqual(route.sensitivity, "sensitive")
        self.assertEqual(route.lane, LANE_LOCAL)

    def test_plain_text_is_internal_and_can_use_cloud(self) -> None:
        route = self.router.route(
            "general_chat",
            text="Explain how TCP handshakes work.",
        )
        self.assertEqual(route.sensitivity, "internal")
        self.assertEqual(route.lane, LANE_CLOUD)


class AuditDetailTests(unittest.TestCase):
    def test_detail_carries_lane_and_class(self) -> None:
        router = LaneRouter()
        route = Route(
            lane=LANE_LOCAL,
            task="email_draft",
            sensitivity="private",
            reason="declared local",
        )
        detail = router.audit_detail(route, model="qwen3.5:2b-q4_K_M")
        self.assertEqual(detail["lane"], LANE_LOCAL)
        self.assertEqual(detail["sensitivity"], "private")
        self.assertEqual(detail["model"], "qwen3.5:2b-q4_K_M")
        self.assertEqual(detail["task"], "email_draft")


if __name__ == "__main__":
    unittest.main()
