"""The control plane's half of the tunnel: dispatch, and what to do when it hangs.

The bridge's job is to never perform a write twice. This module's job is the
harder half — deciding whether to *ask* for one again after the socket dropped.

Three outcomes, three policies:

* ``reject`` — provably not started. Redispatch freely, writes included.
* no ``ack`` before the ack deadline — the bridge never took the work, because
  ack is sent before execution begins. Redispatch freely.
* ``ack`` but no ``result`` — genuinely ambiguous. **Never blind-retry a write.**
  Ask the bridge's ledger (``ledger_query``); if it answers ``unknown``, treat
  the action as performed. Missing one outreach is recoverable; sending a
  stranger two connection requests is not, and it is the customer's account that
  pays for the mistake.

Reads skip all of this: repeating a profile scrape costs a budget slot and
nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from gtmos_schemas.bridge import (
    WRITE_ACTIONS,
    BridgeAck,
    BridgeDispatch,
    BridgeEnvelope,
    BridgeHello,
    BridgeLedgerQuery,
    BridgeLedgerReply,
    BridgeReject,
    BridgeResult,
    BridgeStatus,
)

logger = logging.getLogger(__name__)

_ENVELOPE = TypeAdapter(BridgeEnvelope)

ACK_TIMEOUT_S = 15.0
LEDGER_TIMEOUT_S = 20.0
#: Grace beyond the bridge's own deadline before the cloud gives up waiting for
#: a result. The bridge should always time out first and say so; this only
#: covers the case where it cannot answer at all.
RESULT_GRACE_S = 30.0


class BridgeUnavailable(Exception):
    """No bridge is attached for this tenant, or the one attached refused."""

    def __init__(self, message: str, *, reason: str = "offline", retry_after_s: float | None = None):
        super().__init__(message)
        self.reason = reason
        self.retry_after_s = retry_after_s


class DispatchOutcome:
    """What happened to one dispatch, including the honest 'we don't know'."""

    def __init__(
        self, *, ok: bool, payload: dict | None = None, error: str | None = None,
        error_kind: str | None = None, presumed_performed: bool = False,
    ) -> None:
        self.ok = ok
        self.payload = payload
        self.error = error
        self.error_kind = error_kind
        #: True when a write's fate is unknown and policy says assume it landed.
        self.presumed_performed = presumed_performed


@dataclass
class _Pending:
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Future = field(default_factory=asyncio.Future)
    deduped: bool = False
    rejected: BridgeReject | None = None


class BridgeConnection:
    """One live bridge socket, and the in-flight work on it."""

    def __init__(self, websocket: Any, *, tenant_id: UUID, bridge_id: UUID) -> None:
        self.ws = websocket
        self.tenant_id = tenant_id
        self.bridge_id = bridge_id
        self.hello: BridgeHello | None = None
        self.status: BridgeStatus | None = None
        self._pending: dict[UUID, _Pending] = {}
        self._ledger_waiters: dict[UUID, asyncio.Future] = {}
        # One dispatch at a time: the bridge's profile lease permits no more, so
        # sending concurrently would only queue deeper inside its browser lock
        # while making our own timeouts meaningless.
        self._send_gate = asyncio.Lock()
        self.result_grace_s = RESULT_GRACE_S

    @property
    def is_ready(self) -> bool:
        s = self.status
        return bool(s and s.authenticated and s.browser_ready)

    async def _send(self, frame: Any) -> None:
        await self.ws.send_text(frame.model_dump_json())

    async def handle_frame(self, raw: str) -> None:
        frame = _ENVELOPE.validate_json(raw)
        if isinstance(frame, BridgeHello):
            self.hello = frame
        elif isinstance(frame, BridgeStatus):
            self.status = frame
        elif isinstance(frame, BridgeAck):
            if pending := self._pending.get(frame.dispatch_id):
                pending.deduped = frame.deduped
                pending.acked.set()
        elif isinstance(frame, BridgeReject):
            if pending := self._pending.get(frame.dispatch_id):
                pending.rejected = frame
                pending.acked.set()
                if not pending.done.done():
                    pending.done.set_result(frame)
        elif isinstance(frame, BridgeResult):
            if pending := self._pending.get(frame.dispatch_id):
                pending.acked.set()
                if not pending.done.done():
                    pending.done.set_result(frame)
        elif isinstance(frame, BridgeLedgerReply):
            if waiter := self._ledger_waiters.pop(frame.query_id, None):
                if not waiter.done():
                    waiter.set_result(frame)

    def abandon_inflight(self) -> None:
        """Release everything waiting on a socket that has gone away.

        Without this, a dispatch in flight when the bridge disconnects waits out
        its full deadline before anyone notices, and a ledger query waits
        forever. Waking them now turns a hang into a prompt, accurate failure —
        and for a write, the ack-without-result path still applies, so the run
        is told the outcome is ambiguous rather than being silently retried.
        """
        for pending in self._pending.values():
            pending.acked.set()
            if not pending.done.done():
                pending.done.set_exception(
                    BridgeUnavailable("bridge disconnected mid-dispatch", reason="disconnected")
                )
        for waiter in self._ledger_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._ledger_waiters.clear()

    async def query_ledger(self, keys: list[str]) -> dict[str, str]:
        query = BridgeLedgerQuery(idempotency_keys=keys)
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ledger_waiters[query.query_id] = waiter
        await self._send(query)
        try:
            reply: BridgeLedgerReply = await asyncio.wait_for(waiter, LEDGER_TIMEOUT_S)
        except TimeoutError:
            # Deliberate, not incidental: an unanswered ledger query leaves the
            # caller with no entry, which resolves to "unknown", which for a
            # write means "assume it landed". That is the safe direction, and it
            # is chosen here rather than falling out of a bare except.
            logger.warning(
                "bridge %s did not answer ledger query %s within %.0fs; "
                "treating the write as possibly performed",
                self.bridge_id, query.query_id, LEDGER_TIMEOUT_S,
            )
            return {}
        finally:
            self._ledger_waiters.pop(query.query_id, None)
        return dict(reply.outcome)

    async def dispatch(
        self, *, tool: str, args: dict, run_id: UUID, idempotency_key: str, deadline_s: float = 120.0
    ) -> DispatchOutcome:
        frame = BridgeDispatch(
            idempotency_key=idempotency_key, run_id=run_id, tool=tool,
            args=args, deadline_s=deadline_s,
        )
        pending = _Pending()
        self._pending[frame.dispatch_id] = pending
        try:
            async with self._send_gate:
                await self._send(frame)

            try:
                await asyncio.wait_for(pending.acked.wait(), ACK_TIMEOUT_S)
            except TimeoutError:
                # Ack precedes execution on the bridge, so its absence proves the
                # work never began. Safe to redispatch, writes included.
                raise BridgeUnavailable(
                    f"bridge did not acknowledge {tool} within {ACK_TIMEOUT_S}s",
                    reason="no_ack",
                ) from None

            if pending.rejected is not None:
                r = pending.rejected
                raise BridgeUnavailable(
                    f"bridge refused {tool}: {r.reason}"
                    + (f" ({r.detail})" if r.detail else ""),
                    reason=r.reason,
                    retry_after_s=r.retry_after_s,
                )

            try:
                outcome = await asyncio.wait_for(pending.done, deadline_s + self.result_grace_s)
            except (TimeoutError, asyncio.CancelledError):
                return await self._resolve_ambiguity(tool, idempotency_key)

            assert isinstance(outcome, BridgeResult)
            return DispatchOutcome(
                ok=outcome.ok, payload=outcome.payload,
                error=outcome.error, error_kind=outcome.error_kind,
            )
        finally:
            self._pending.pop(frame.dispatch_id, None)

    async def _resolve_ambiguity(self, tool: str, key: str) -> DispatchOutcome:
        """Acked, never finished. Ask; never assume."""
        if tool not in WRITE_ACTIONS:
            return DispatchOutcome(
                ok=False, error=f"{tool} did not return in time", error_kind="timeout"
            )
        outcome = (await self.query_ledger([key])).get(key, "unknown")
        if outcome == "not_performed":
            return DispatchOutcome(
                ok=False, error=f"{tool} failed before taking effect", error_kind="timeout"
            )
        # 'performed' or 'unknown' both stop here. Treating unknown as performed
        # is the deliberate asymmetry: a missed message can be sent later, a
        # duplicate connection request cannot be unsent.
        return DispatchOutcome(
            ok=False,
            error=f"{tool} may already have been delivered; not retrying",
            error_kind="ambiguous",
            presumed_performed=True,
        )


class BridgeRegistry:
    """Which bridge, if any, is currently attached for each tenant."""

    def __init__(self) -> None:
        self._by_tenant: dict[UUID, BridgeConnection] = {}
        self._lock = asyncio.Lock()

    async def attach(self, conn: BridgeConnection) -> None:
        async with self._lock:
            if existing := self._by_tenant.get(conn.tenant_id):
                # Last writer wins, but the displaced socket is closed rather
                # than left half-alive holding a browser lease nobody can reach.
                with contextlib.suppress(Exception):
                    await existing.ws.close()
            self._by_tenant[conn.tenant_id] = conn
            logger.info("bridge %s attached for tenant %s", conn.bridge_id, conn.tenant_id)

    async def detach(self, conn: BridgeConnection) -> None:
        conn.abandon_inflight()
        async with self._lock:
            if self._by_tenant.get(conn.tenant_id) is conn:
                del self._by_tenant[conn.tenant_id]
                logger.info("bridge %s detached for tenant %s", conn.bridge_id, conn.tenant_id)

    def get(self, tenant_id: UUID) -> BridgeConnection | None:
        return self._by_tenant.get(tenant_id)

    def require(self, tenant_id: UUID) -> BridgeConnection:
        conn = self.get(tenant_id)
        if conn is None:
            raise BridgeUnavailable(
                "No LinkedIn bridge is connected. Run `gtm-bridge run` on the machine "
                "with your LinkedIn session, or `gtm-bridge pair` if this is the first time.",
                reason="offline",
            )
        return conn

    def new_idempotency_key(self, *, tenant_id: UUID, person_ref: str, action: str) -> str:
        """Stable per (tenant, person, action) so a redispatch reuses it."""
        return f"{tenant_id}:{action}:{person_ref}"

    @staticmethod
    def fresh_key() -> str:
        return str(uuid4())
