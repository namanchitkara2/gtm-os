"""Wire protocol between the cloud control plane and a user's local bridge.

The bridge dials *out* over WSS. That is not a style choice: the upstream
project's owner role listens on loopback (``daemon_proxy.py``), which is
unreachable from AWS, and asking customers to open an inbound port on a laptop
is not a product. Outbound also means the tunnel survives NAT, coffee-shop wifi,
and corporate egress proxies unchanged.

**The send-once problem, and why ``ack`` is a separate message.**

``daemon_proxy`` states the rule the local version relies on: *only a request
that provably never left this process may be repeated*, because repeating a
connection request sends it twice. In-process that is decidable. Across a
socket it is not — a dropped connection after ``dispatch`` and before ``result``
is genuinely ambiguous, and both possible answers are wrong to assume.

So the protocol makes it decidable:

* no ``ack`` received  → the bridge never took the work → **safe to redispatch**
* ``ack`` but no ``result`` → ambiguous → **never blind-retry a write**; the
  cloud must ask the bridge's action ledger (``ledger_query``) what actually
  happened once the bridge returns.

The ledger is mirrored server-side as well, keyed by (tenant, person urn,
action), because a bridge that is re-paired on a new machine comes back with an
empty local ledger and would otherwise happily send again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Bridge-side handlers that mutate something on LinkedIn. Everything not in
#: this set is a read and may be retried freely.
WRITE_ACTIONS: frozenset[str] = frozenset({"connect_with_person", "send_message"})

#: Reads on non-LinkedIn sites, served by the Stagehand web agent. Two rules
#: follow from that: they never touch LinkedIn, so they do not spend the
#: LinkedIn daily budget; and they are all reads. ``web_act`` is deliberately
#: absent — it can submit a form, so it may only be offered once it is in
#: WRITE_ACTIONS and the ledger covers it.
WEB_TOOLS: frozenset[str] = frozenset({"web_navigate", "web_extract", "web_observe", "web_page"})


class BridgeHello(BaseModel):
    """First frame after the socket opens. Establishes who and what version."""

    type: Literal["hello"] = "hello"
    bridge_id: UUID
    bridge_version: str
    #: Which LinkedIn account this bridge drives, from ``get_my_profile``. The
    #: cloud pins runs to it so a user swapping accounts mid-campaign is caught
    #: rather than silently mixing two identities into one outreach history.
    linkedin_username: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    platform: str | None = None


class BridgeStatus(BaseModel):
    """Heartbeat. Absence of these is how the cloud parks a run as awaiting_bridge."""

    type: Literal["status"] = "status"
    bridge_id: UUID
    authenticated: bool
    browser_ready: bool
    #: True while the profile lease is held, i.e. a call is in flight. The
    #: cloud uses this to avoid queueing behind a bridge that is already busy;
    #: it cannot parallelise, because the lease forbids it.
    busy: bool = False
    actions_today: int = 0
    last_error: str | None = None
    sent_at: datetime = Field(default_factory=_utcnow)


class BridgeDispatch(BaseModel):
    """Cloud → bridge: run one LinkedIn handler."""

    type: Literal["dispatch"] = "dispatch"
    dispatch_id: UUID = Field(default_factory=uuid4)
    #: Stable across redispatch of the same logical action. The bridge's ledger
    #: is keyed on this, so a redispatch after a socket drop is a no-op rather
    #: than a second connection request.
    idempotency_key: str
    run_id: UUID
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    deadline_s: float = 120.0

    @property
    def is_write(self) -> bool:
        return self.tool in WRITE_ACTIONS


class BridgeAck(BaseModel):
    """Bridge → cloud: I have this dispatch and I am starting it.

    The whole point of a separate frame. Without it the cloud cannot tell
    "never arrived" from "arrived and may have executed".
    """

    type: Literal["ack"] = "ack"
    dispatch_id: UUID
    #: True when the ledger already had this idempotency key, i.e. the action
    #: was performed on an earlier attempt and is not being repeated.
    deduped: bool = False


class BridgeReject(BaseModel):
    """Bridge → cloud: I will not run this, and I have not started it.

    The negative form of ``ack``, and the protocol is unsafe without it. A bridge
    that cannot run a dispatch — not authenticated, browser dead, budget spent,
    tool it does not know — otherwise has two bad options: stay silent, which is
    indistinguishable from "never arrived" and so destroys the very ambiguity
    ``ack`` exists to remove; or ack-then-fail, which burns a daily budget slot
    on work never attempted and makes a write look ambiguous when it is not.

    ``reject`` means *provably not started*. The cloud may redispatch it freely,
    including a write, with no ledger query.
    """

    type: Literal["reject"] = "reject"
    dispatch_id: UUID
    reason: Literal[
        "unauthenticated",
        "browser_unavailable",
        "budget_exhausted",
        "unknown_tool",
        "busy",
        "shutting_down",
    ]
    detail: str | None = None
    #: When the cloud may sensibly try again; None means "not without user action".
    retry_after_s: float | None = None


class BridgeResult(BaseModel):
    """Bridge → cloud: terminal outcome of one dispatch."""

    type: Literal["result"] = "result"
    dispatch_id: UUID
    ok: bool
    #: Raw handler output. For reads this is the upstream ``{url, sections}``
    #: shape; structuring it into a Person happens cloud-side, not here, so the
    #: bridge stays a thin arm and model spend stays on the control plane.
    payload: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = Field(
        default=None,
        description="auth | rate_limited | not_found | browser | timeout | unknown",
    )
    duration_ms: int | None = None


class BridgeLedgerQuery(BaseModel):
    """Cloud → bridge: did this write actually happen?

    Sent after an ack-without-result. Never guess; ask.
    """

    type: Literal["ledger_query"] = "ledger_query"
    query_id: UUID = Field(default_factory=uuid4)
    idempotency_keys: list[str]


#: What the bridge's ledger can honestly say about a past write.
#: ``unknown`` is not a failure of bookkeeping — it is the true answer when the
#: bridge recorded the start of an action and then died before recording its
#: end. Collapsing it into either certainty is how a connection request gets
#: sent twice (or silently dropped), so the protocol carries it explicitly and
#: lets the cloud apply policy: for writes, treat unknown as performed, because
#: missing one outreach is recoverable and double-sending one is not.
LedgerOutcome = Literal["performed", "not_performed", "unknown"]


class BridgeLedgerReply(BaseModel):
    type: Literal["ledger_reply"] = "ledger_reply"
    query_id: UUID
    #: idempotency_key -> what the ledger knows about it.
    outcome: dict[str, LedgerOutcome] = Field(default_factory=dict)


BridgeEnvelope = Annotated[
    BridgeHello
    | BridgeStatus
    | BridgeDispatch
    | BridgeAck
    | BridgeReject
    | BridgeResult
    | BridgeLedgerQuery
    | BridgeLedgerReply,
    Field(discriminator="type"),
]
