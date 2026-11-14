"""Lane routing: the privacy boundary as code, not as a prompt.

PRD section 10.5 is a table, and this module is that table.  A request names its
task; the router combines that with the payload's sensitivity class and returns
exactly one lane:

    local    Ollama on loopback.  Private content lives here and cannot leave.
    cloud    A remote provider, allowed only for public or internal payloads.
    refusal  No model at all.  Secret-class content gets a manual path.

The rule that matters is the negative one: when the local lane fails, there is
no cloud fallback for private content.  ``Route.fallback`` is deliberately
empty for every local task, so a caller that follows the route cannot leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import sensitivity

LANE_LOCAL = "local"
LANE_CLOUD = "cloud"
LANE_REFUSAL = "refusal"
LANES = (LANE_LOCAL, LANE_CLOUD, LANE_REFUSAL)

# Task -> (lane, fallback).  Mirrors the PRD table row for row.
TASK_RULES: dict[str, tuple[str, str | None]] = {
    "email_classification": (LANE_LOCAL, None),
    "email_extraction": (LANE_LOCAL, None),
    "email_draft": (LANE_LOCAL, None),
    "calendar_summary": (LANE_LOCAL, None),
    "planning": (LANE_LOCAL, None),
    "general_chat": (LANE_CLOUD, LANE_LOCAL),
    "web_research": (LANE_CLOUD, LANE_LOCAL),
}

DEFAULT_TASK = "general_chat"


def task_lane(task: str) -> tuple[str, str | None]:
    """Return the declared lane for a task, defaulting to general chat."""
    return TASK_RULES.get(str(task or "").strip(), TASK_RULES[DEFAULT_TASK])


@dataclass(frozen=True)
class Route:
    """One routing decision, with the reason recorded for the audit log."""

    lane: str
    task: str
    sensitivity: str
    reason: str
    fallback: str | None = None

    @property
    def is_refusal(self) -> bool:
        return self.lane == LANE_REFUSAL

    @property
    def is_local(self) -> bool:
        return self.lane == LANE_LOCAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "task": self.task,
            "sensitivity": self.sensitivity,
            "reason": self.reason,
            "fallback": self.fallback,
        }


class LaneRouter:
    """Apply the PRD's routing table to a task and a payload."""

    def __init__(
        self,
        *,
        allow_cloud_for: tuple[str, ...] | list[str] = ("public", "internal"),
        refuse: tuple[str, ...] | list[str] = ("secret",),
        default_lane: str = LANE_LOCAL,
    ) -> None:
        self.allow_cloud_for = tuple(
            sensitivity.parse(name) for name in allow_cloud_for
        )
        self.refuse = tuple(sensitivity.parse(name) for name in refuse)
        self.default_lane = (
            default_lane if default_lane in LANES else LANE_LOCAL
        )

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------
    def decide(self, task: str, sensitivity_class: str) -> Route:
        """Route one task whose payload class is already known."""
        name = str(task or DEFAULT_TASK).strip() or DEFAULT_TASK
        body = sensitivity.parse(sensitivity_class)
        lane, fallback = task_lane(name)

        if body in self.refuse:
            return Route(
                lane=LANE_REFUSAL,
                task=name,
                sensitivity=body,
                reason=(
                    f"{body} content is never sent to a model (FR-P3); "
                    "handle it manually."
                ),
                fallback=None,
            )

        if sensitivity.is_local_only(body) and body not in self.allow_cloud_for:
            return Route(
                lane=LANE_LOCAL,
                task=name,
                sensitivity=body,
                reason=(
                    f"{body} payload must stay on the local lane (FR-P2); "
                    "no cloud fallback."
                ),
                fallback=None,
            )

        if lane == LANE_CLOUD:
            if body not in self.allow_cloud_for:
                return Route(
                    lane=self.default_lane,
                    task=name,
                    sensitivity=body,
                    reason=(
                        f"{body} payload is outside allow_cloud_for, so the "
                        "default lane applies."
                    ),
                    fallback=None,
                )
            return Route(
                lane=LANE_CLOUD,
                task=name,
                sensitivity=body,
                reason=f"{name} is a cloud task and the payload is {body}.",
                fallback=fallback,
            )

        return Route(
            lane=lane,
            task=name,
            sensitivity=body,
            reason=f"{name} is declared local by the routing table.",
            fallback=None,
        )

    def route(
        self,
        task: str = DEFAULT_TASK,
        *,
        text: str = "",
        declared: str | None = None,
    ) -> Route:
        """Classify the payload text, then decide.

        A declared class is a floor: the router takes the more private of the
        declaration and what the text actually looks like.
        """
        inferred = sensitivity.classify(text, default=declared or sensitivity.DEFAULT)
        body = sensitivity.highest(declared, inferred) if declared else inferred
        return self.decide(task, body)

    def audit_detail(
        self,
        route: Route,
        *,
        model: str = "",
        status: str = "ok",
        detail: str = "",
    ) -> dict[str, Any]:
        payload = route.as_dict()
        payload["model"] = model
        payload["status"] = status
        if detail:
            payload["detail"] = detail
        return payload
