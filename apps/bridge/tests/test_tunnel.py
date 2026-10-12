"""Dispatch semantics under failure, against a real WebSocket server.

No browser and no LinkedIn account: the fake executor lets these run in
milliseconds, which is what makes it reasonable to test every branch of the
path that can otherwise send a connection request twice.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import websockets
from pydantic import TypeAdapter

from gtmos_bridge.budget import DailyBudget
from gtmos_bridge.executor import ExecutorError
from gtmos_bridge.ledger import ActionLedger
from gtmos_bridge.tunnel import Tunnel
from gtmos_schemas.bridge import (
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

_ENV = TypeAdapter(BridgeEnvelope)
TOOLS = {"get_person_profile", "send_message", "connect_with_person"}


class FakeExecutor:
    """Records calls; can be told to fail, hang, or report unhealthy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.authenticated = True
        self.browser_ready = True
        self.raise_exc: Exception | None = None
        self.hang = asyncio.Event()
        self.hang.set()  # not hanging by default

    def known_tools(self) -> set[str]:
        return set(TOOLS)

    async def health(self) -> tuple[bool, bool]:
        return self.authenticated, self.browser_ready

    async def call(self, tool: str, args: dict, *, timeout_s: float) -> dict:
        self.calls.append((tool, args))
        await self.hang.wait()
        if self.raise_exc:
            raise self.raise_exc
        return {"ok": True, "tool": tool}


class Harness:
    """A control plane that scripts frames at a bridge and collects replies."""

    def __init__(self, tmp: Path, *, limit: int = 100, executor=None):
        self.exec = executor or FakeExecutor()
        self.ledger = ActionLedger(tmp / "ledger.db")
        self.budget = DailyBudget(tmp / "budget.db", limit=limit)
        self.received: asyncio.Queue = asyncio.Queue()
        self._server = None
        self._task = None

    async def __aenter__(self):
        ready = asyncio.Event()
        self.to_bridge: asyncio.Queue = asyncio.Queue()

        async def handler(ws):
            ready.set()
            async def pump():
                while True:
                    frame = await self.to_bridge.get()
                    await ws.send(frame.model_dump_json())
            p = asyncio.create_task(pump())
            try:
                async for raw in ws:
                    await self.received.put(_ENV.validate_json(raw))
            finally:
                p.cancel()

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.tunnel = Tunnel(
            f"ws://127.0.0.1:{port}", "tok", bridge_id=uuid4(), version="test",
            executor=self.exec, ledger=self.ledger, budget=self.budget,
        )
        self._task = asyncio.create_task(self.tunnel.run_forever(max_attempts=1))
        await asyncio.wait_for(ready.wait(), 5)
        hello = await self.expect(BridgeHello)
        assert set(hello.capabilities) == self.exec.known_tools()
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        self._server.close()
        await self._server.wait_closed()

    async def send(self, frame): await self.to_bridge.put(frame)

    async def expect(self, kind, timeout=5.0):
        """Next frame of the given type, skipping heartbeats."""
        async with asyncio.timeout(timeout):
            while True:
                f = await self.received.get()
                if isinstance(f, kind):
                    return f
                if not isinstance(f, BridgeStatus):
                    raise AssertionError(f"expected {kind.__name__}, got {type(f).__name__}: {f}")


def dispatch(tool="get_person_profile", key=None, **args) -> BridgeDispatch:
    return BridgeDispatch(
        idempotency_key=key or str(uuid4()), run_id=uuid4(), tool=tool, args=args
    )


# ------------------------------------------------------------------ the happy path --

async def test_read_dispatch_acks_then_returns_result(tmp_path):
    async with Harness(tmp_path) as h:
        await h.send(dispatch(linkedin_username="janeroe"))
        ack = await h.expect(BridgeAck)
        assert ack.deduped is False
        res = await h.expect(BridgeResult)
        assert res.ok and res.payload["tool"] == "get_person_profile"
        assert len(h.exec.calls) == 1


# ------------------------------------------------------- refusals: provably not started --

@pytest.mark.parametrize("setup,reason", [
    (lambda e: setattr(e, "authenticated", False), "unauthenticated"),
    (lambda e: setattr(e, "browser_ready", False), "browser_unavailable"),
])
async def test_unhealthy_bridge_rejects_without_acking(tmp_path, setup, reason):
    async with Harness(tmp_path) as h:
        setup(h.exec)
        await h.send(dispatch())
        rej = await h.expect(BridgeReject)
        assert rej.reason == reason
        assert h.exec.calls == [], "a rejected dispatch must never execute"
        assert h.budget.used_today() == 0, "a rejected dispatch must not spend budget"


async def test_unknown_tool_is_rejected(tmp_path):
    async with Harness(tmp_path) as h:
        await h.send(dispatch(tool="delete_everything"))
        rej = await h.expect(BridgeReject)
        assert rej.reason == "unknown_tool"
        assert h.exec.calls == []


async def test_budget_exhaustion_rejects_rather_than_failing(tmp_path):
    async with Harness(tmp_path, limit=1) as h:
        await h.send(dispatch())
        await h.expect(BridgeAck); await h.expect(BridgeResult)
        await h.send(dispatch())
        rej = await h.expect(BridgeReject)
        assert rej.reason == "budget_exhausted" and rej.retry_after_s == 3600.0
        assert len(h.exec.calls) == 1


# ------------------------------------------------------------- the send-once guarantee --

async def test_redispatched_write_is_deduped_and_never_sent_twice(tmp_path):
    """The case this whole protocol exists for: the cloud retries after a drop."""
    async with Harness(tmp_path) as h:
        key = "same-key"
        await h.send(dispatch(tool="send_message", key=key, linkedin_username="janeroe"))
        await h.expect(BridgeAck); r1 = await h.expect(BridgeResult)
        assert r1.ok and len(h.exec.calls) == 1

        await h.send(dispatch(tool="send_message", key=key, linkedin_username="janeroe"))
        ack2 = await h.expect(BridgeAck)
        r2 = await h.expect(BridgeResult)
        assert ack2.deduped is True, "a repeat write must be flagged as deduped"
        assert r2.ok and r2.payload["ledger_outcome"] == "performed"
        assert len(h.exec.calls) == 1, "THE MESSAGE MUST NOT BE SENT TWICE"


async def test_reads_are_not_deduped(tmp_path):
    """Reads are safe to repeat; deduping them would serve stale data forever."""
    async with Harness(tmp_path) as h:
        for _ in range(2):
            await h.send(dispatch(key="k", linkedin_username="janeroe"))
            await h.expect(BridgeAck); await h.expect(BridgeResult)
        assert len(h.exec.calls) == 2


async def test_classified_failure_marks_write_retryable(tmp_path):
    async with Harness(tmp_path) as h:
        h.exec.raise_exc = ExecutorError("429", kind="rate_limited")
        await h.send(dispatch(tool="connect_with_person", key="k1"))
        await h.expect(BridgeAck)
        res = await h.expect(BridgeResult)
        assert not res.ok and res.error_kind == "rate_limited"
        assert h.ledger.outcome("k1") == "not_performed", "a classified failure is safe to retry"
        assert h.budget.used_today() == 0, "budget must be refunded when nothing happened"


async def test_unclassified_crash_leaves_write_unknown(tmp_path):
    """The ambiguous case must stay ambiguous rather than being guessed."""
    async with Harness(tmp_path) as h:
        h.exec.raise_exc = RuntimeError("socket died mid-send")
        await h.send(dispatch(tool="send_message", key="k2"))
        await h.expect(BridgeAck)
        res = await h.expect(BridgeResult)
        assert not res.ok and res.error_kind == "unknown"
        assert h.ledger.outcome("k2") == "unknown", (
            "an unclassified crash must NOT be recorded as not-performed"
        )


# ------------------------------------------------- the reader must never block --

async def test_ledger_query_is_answered_while_a_dispatch_is_in_flight(tmp_path):
    """The cloud asks about a stuck write exactly when one is stuck."""
    async with Harness(tmp_path) as h:
        h.exec.hang.clear()  # make the dispatch hang mid-execution
        await h.send(dispatch(tool="send_message", key="stuck"))
        await h.expect(BridgeAck)

        await h.send(BridgeLedgerQuery(idempotency_keys=["stuck", "never"]))
        reply = await h.expect(BridgeLedgerReply, timeout=3.0)
        assert reply.outcome == {"stuck": "unknown", "never": "not_performed"}, (
            "a write still executing is 'unknown', and the reader answered while it ran"
        )
        h.exec.hang.set()
        await h.expect(BridgeResult)


async def test_malformed_frame_does_not_kill_the_session(tmp_path):
    async with Harness(tmp_path) as h:
        await h.to_bridge.put(type("Raw", (), {"model_dump_json": lambda self: "{not json"})())
        await h.send(dispatch())
        await h.expect(BridgeAck)
        assert len(h.exec.calls) == 1, "session survived a garbage frame"
