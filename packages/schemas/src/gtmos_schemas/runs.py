"""Runs and their step traces.

The ``run_steps`` table this mirrors is the source of truth for the trace UI,
deliberately rather than LangSmith: the traces are a product surface customers
look at, not an ops tool, so they cannot live behind a third-party dashboard or
disappear when a vendor plan lapses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

RunStatus = Literal[
    "queued",
    "running",
    "awaiting_bridge",
    "awaiting_approval",
    "succeeded",
    "failed",
    "cancelled",
]

StepStatus = Literal["running", "succeeded", "failed", "skipped", "cached"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRef(BaseModel):
    """One entry in the prebuilt workflow library."""

    slug: str
    name: str
    pack: Literal["sales", "recruiting"]
    description: str
    target_kind: Literal["icp", "role"]
    requires_bridge: bool = Field(
        description="True when the graph touches LinkedIn and so needs a paired bridge"
    )
    requires_providers: list[str] = Field(
        default_factory=list, description="BYOK providers that must be configured"
    )


class RunStep(BaseModel):
    """One graph node execution.

    ``cached`` is a first-class status rather than a flag on ``succeeded``
    because "we did not spend a LinkedIn call on this" is the single most
    important thing the trace can tell a user, given the one-action-at-a-time
    ceiling.
    """

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    node: str
    status: StepStatus
    sequence: int
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None

    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    #: LinkedIn actions this step spent from the tenant's daily budget.
    bridge_calls: int = 0
    provider_credits: dict[str, int] = Field(default_factory=dict)

    retries: int = 0
    error: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


class Run(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    user_id: UUID
    workflow: str
    target_id: UUID | None = None
    status: RunStatus = "queued"

    created_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    #: LangGraph thread id. Resuming a run means resuming this thread from the
    #: Postgres checkpointer, which is why it is persisted alongside the run.
    thread_id: str | None = None

    limits: dict[str, int] = Field(
        default_factory=dict,
        description="Per-run caps, e.g. {'max_people': 25, 'max_bridge_calls': 40}",
    )
    progress: dict[str, int] = Field(
        default_factory=dict, description="e.g. {'sourced': 25, 'enriched': 12}"
    )

    error: str | None = None
    #: Set when status is awaiting_bridge or awaiting_approval, so the client
    #: gets an actionable message instead of a run that merely looks stuck.
    blocked_reason: str | None = None

    total_cost_usd: float = 0.0
    total_bridge_calls: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")
