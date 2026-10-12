"""The outbound tunnel, and the decision table that keeps writes send-once.

The socket dials out. Everything else in this module exists to answer one
question correctly under failure: *did that action happen?*

Order of checks on an incoming dispatch, and why this order:

1. **Refusals first, before anything is recorded or spent.** Unknown tool, not
   authenticated, no browser, no budget — each produces ``reject``, which means
   *provably not started*. The cloud may redispatch freely. Rejecting before
   touching the ledger or the budget is what keeps that promise true.
2. **Ledger claim, before executing.** ``begin()`` is the guard. If the key is
   already claimed, the action was attempted before: ack with ``deduped`` and
   return the fact rather than doing it again. This is the check that survives a
   socket drop, a redispatch, and a control-plane restart.
3. **Ack, then execute.** Once acked, the cloud must never blind-retry a write;
   it must ask the ledger. That contract is the reason ack is a separate frame.

The reader loop never executes a dispatch itself. Dispatches go to a queue
drained by a single worker — single because the upstream profile lease permits
exactly one browser action at a time anyway, so any concurrency here would only
queue deeper inside someone else's lock. Keeping the reader free means a
``ledger_query`` can still be answered while a long scrape is in flight, which
is precisely when the cloud most needs to ask it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any
from uuid import UUID

import websockets
from pydantic import TypeAdapter

from gtmos_schemas.bridge import (
    WEB_TOOLS,
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

from gtmos_bridge.budget import DailyBudget
from gtmos_bridge.executor import Executor, ExecutorError
from gtmos_bridge.ledger import ActionLedger

logger = logging.getLogger(__name__)

_ENVELOPE = TypeAdapter(BridgeEnvelope)

HEARTBEAT_S = 20.0


def person_ref(tool: str, args: dict[str, Any]) -> str | None:
    """The person a write action targets, for the ledger's human-readable trail."""
    for key in ("linkedin_username", "person_urn", "username", "recipient"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class Tunnel:
    """One live connection to the control plane, with reconnect."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        bridge_id: UUID,
        version: str,
        executor: Executor,
        ledger: ActionLedger,
        budget: DailyBudget,
        linkedin_username: str | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._bridge_id = bridge_id
        self._version = version
        self._executor = executor
        self._ledger = ledger
        self._budget = budget
        self._linkedin_username = linkedin_username
        self._queue: asyncio.Queue[BridgeDispatch] = asyncio.Queue()
        self._ws: Any = None
        self._busy = False
        self._last_error: str | None = None

    # ------------------------------------------------------------ connection --

    async def run_forever(self, *, max_attempts: int | None = None) -> None:
        """Connect, serve, and reconnect with jittered backoff.

        A bridge that gives up on the first network blip is a bridge that parks
        every run on the user's laptop lid. Backoff is capped so a reconnect
        after a long sleep is still prompt.
        """
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            attempt += 1
            try:
                await self._session()
                attempt = 0  # a clean session resets the penalty
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                delay = min(60.0, 2 ** min(attempt, 6)) * (0.5 + random.random())
                logger.warning("tunnel disconnected (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _session(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with websockets.connect(self._url, additional_headers=headers) as ws:
            self._ws = ws
            logger.info("tunnel connected to %s", self._url)
            await self._send(
                BridgeHello(
                    bridge_id=self._bridge_id,
                    bridge_version=self._version,
                    linkedin_username=self._linkedin_username,
                    capabilities=sorted(self._executor.known_tools()),
                )
            )
            worker = asyncio.create_task(self._drain_dispatches())
            beat = asyncio.create_task(self._heartbeat())
            try:
                async for raw in ws:
                    await self._on_message(raw)
            finally:
                for task in (worker, beat):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                self._ws = None

    async def _send(self, frame: Any) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("tunnel not connected")
        await ws.send(frame.model_dump_json())

    async def _heartbeat(self) -> None:
        while True:
            authenticated, browser_ready = await self._executor.health()
            with contextlib.suppress(Exception):
                await self._send(
                    BridgeStatus(
                        bridge_id=self._bridge_id,
                        authenticated=authenticated,
                        browser_ready=browser_ready,
                        busy=self._busy,
                        actions_today=self._budget.used_today(),
                        last_error=self._last_error,
                    )
                )
            await asyncio.sleep(HEARTBEAT_S)

    # -------------------------------------------------------------- messages --

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            frame = _ENVELOPE.validate_json(raw)
        except Exception:
            logger.warning("dropping unparseable frame from control plane")
            return

        if isinstance(frame, BridgeDispatch):
            await self._queue.put(frame)
        elif isinstance(frame, BridgeLedgerQuery):
            # Answered on the reader task, deliberately: the cloud asks this
            # precisely when a dispatch is stuck, so it must not queue behind it.
            await self._send(
                BridgeLedgerReply(
                    query_id=frame.query_id,
                    outcome=self._ledger.outcomes(frame.idempotency_keys),
                )
            )
        else:
            logger.debug("ignoring %s frame from control plane", type(frame).__name__)

    async def _drain_dispatches(self) -> None:
        while True:
            dispatch = await self._queue.get()
            try:
                await self._handle_dispatch(dispatch)
            except Exception:
                logger.exception("dispatch %s crashed the worker loop", dispatch.dispatch_id)

    # -------------------------------------------------------------- dispatch --

    async def _handle_dispatch(self, d: BridgeDispatch) -> None:
        reject = await self._refusal_for(d)
        if reject is not None:
            await self._send(reject)
            return

        is_write = d.tool in WRITE_ACTIONS
        # The daily budget protects the LinkedIn account. A web read never
        # touches it, so a portal scrape must not eat into outreach capacity.
        charged = d.tool not in WEB_TOOLS

        if is_write and not self._ledger.begin(
            d.idempotency_key, tool=d.tool, person_ref=person_ref(d.tool, d.args)
        ):
            # Already attempted. Not an error — this is the guard working.
            outcome = self._ledger.outcome(d.idempotency_key)
            await self._send(BridgeAck(dispatch_id=d.dispatch_id, deduped=True))
            await self._send(
                BridgeResult(
                    dispatch_id=d.dispatch_id,
                    ok=outcome != "not_performed",
                    payload={"deduped": True, "ledger_outcome": outcome},
                    error=None if outcome != "not_performed" else "previous attempt failed",
                )
            )
            return

        if charged and not self._budget.try_spend():
            if is_write:
                self._ledger.complete(d.idempotency_key, ok=False, error="budget exhausted")
            await self._send(
                BridgeReject(
                    dispatch_id=d.dispatch_id,
                    reason="budget_exhausted",
                    detail=f"{self._budget.limit} actions/day already spent",
                    retry_after_s=3600.0,
                )
            )
            return

        await self._send(BridgeAck(dispatch_id=d.dispatch_id))

        self._busy = True
        try:
            payload = await self._executor.call(d.tool, d.args, timeout_s=d.deadline_s)
        except ExecutorError as exc:
            if charged:
                self._budget.refund()
            if is_write:
                # Only a classified failure proves nothing happened. Anything
                # else must stay 'started' so the ledger can say "unknown".
                if exc.kind in ("rate_limited", "not_found", "auth"):
                    self._ledger.complete(d.idempotency_key, ok=False, error=str(exc))
            await self._send(
                BridgeResult(
                    dispatch_id=d.dispatch_id, ok=False, error=str(exc), error_kind=exc.kind
                )
            )
        except Exception as exc:
            if charged:
                self._budget.refund()
            # Deliberately leaves a write's ledger row at 'started': an
            # unclassified crash is exactly the case where we cannot claim the
            # action did not take effect.
            await self._send(
                BridgeResult(
                    dispatch_id=d.dispatch_id, ok=False, error=str(exc), error_kind="unknown"
                )
            )
        else:
            if is_write:
                self._ledger.complete(d.idempotency_key, ok=True)
            await self._send(BridgeResult(dispatch_id=d.dispatch_id, ok=True, payload=payload))
        finally:
            self._busy = False

    async def _refusal_for(self, d: BridgeDispatch) -> BridgeReject | None:
        if d.tool not in self._executor.known_tools():
            return BridgeReject(
                dispatch_id=d.dispatch_id, reason="unknown_tool", detail=d.tool
            )
        # Per-tool when the executor can say: a LinkedIn checkpoint must not
        # refuse a web read, nor a stalled web agent a LinkedIn one.
        health_for = getattr(self._executor, "health_for", None)
        authenticated, browser_ready = await (
            health_for(d.tool) if health_for is not None else self._executor.health()
        )
        if not authenticated:
            return BridgeReject(
                dispatch_id=d.dispatch_id,
                reason="unauthenticated",
                detail="LinkedIn session is not valid; run `gtm-bridge login`",
            )
        if not browser_ready:
            return BridgeReject(
                dispatch_id=d.dispatch_id, reason="browser_unavailable", retry_after_s=30.0
            )
        return None
