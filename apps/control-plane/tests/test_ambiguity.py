"""The ack-without-result path: the only reason BridgeAck is a separate frame.

A real Tunnel on one side, the real BridgeConnection on the other, a real
WebSocket between them. The bridge's executor is made to hang, which is exactly
what a browser wedged on a LinkedIn challenge looks like from the cloud.

An earlier version of the end-to-end script claimed to cover this and did not:
it pre-populated the ledger and observed the dedup short-circuit, which is a
different branch entirely. This is the one that decides whether a customer's
prospect gets two connection requests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from gtmos_bridge.budget import DailyBudget
from gtmos_bridge.ledger import ActionLedger
from gtmos_bridge.tunnel import Tunnel
from gtmos_control.bridges import BridgeConnection, BridgeUnavailable

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "tests"))
from test_tunnel import FakeExecutor  # noqa: E402  the same fake, deliberately


class Rig:
    """A real control plane socket and a real bridge, wired together."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.executor = FakeExecutor()
        self.conn: BridgeConnection | None = None
        self._attached = asyncio.Event()

    async def __aenter__(self) -> Rig:
        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            self.conn = BridgeConnection(websocket, tenant_id=uuid4(), bridge_id=uuid4())
            self.conn.result_grace_s = 1.0  # tests should not wait out the production grace
            self._attached.set()
            try:
                while True:
                    await self.conn.handle_frame(await websocket.receive_text())
            except (WebSocketDisconnect, RuntimeError):
                pass

        app = Starlette(routes=[WebSocketRoute("/bridge/ws", endpoint)])
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
        )
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]

        self.ledger = ActionLedger(self.tmp / "l.db")
        self.tunnel = Tunnel(
            f"ws://127.0.0.1:{port}/bridge/ws", "tok", bridge_id=uuid4(), version="t",
            executor=self.executor, ledger=self.ledger,
            budget=DailyBudget(self.tmp / "b.db", limit=100),
        )
        self._tunnel_task = asyncio.create_task(self.tunnel.run_forever(max_attempts=1))
        await asyncio.wait_for(self._attached.wait(), 5)
        # Wait for the first status frame so the connection reports ready.
        for _ in range(200):
            if self.conn.status is not None:
                break
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc) -> None:
        self._tunnel_task.cancel()
        self._server.should_exit = True
        await asyncio.gather(self._task, return_exceptions=True)


async def test_hung_write_is_presumed_performed_never_retried(tmp_path):
    """Acked, then silence. The cloud must ask the ledger and refuse to retry."""
    async with Rig(tmp_path) as rig:
        rig.executor.hang.clear()  # the browser wedges mid-send

        outcome = await rig.conn.dispatch(
            tool="send_message",
            args={"linkedin_username": "janeroe", "message": "hi"},
            run_id=uuid4(),
            idempotency_key="ambiguous-1",
            deadline_s=0.5,  # forces the ack-without-result path quickly
        )

        assert outcome.ok is False
        assert outcome.error_kind == "ambiguous", (
            "a write that was acked and never finished must be reported as ambiguous, "
            "not as a plain timeout the caller might retry"
        )
        assert outcome.presumed_performed is True, (
            "policy: an unknown write is assumed delivered — a duplicate connection "
            "request cannot be unsent, a missed one can be re-sent"
        )
        assert len(rig.executor.calls) == 1, "THE MESSAGE MUST NOT BE SENT TWICE"
        assert rig.ledger.outcome("ambiguous-1") == "unknown", (
            "the bridge ledger must still say unknown while the action is in flight"
        )

        rig.executor.hang.set()
        await asyncio.sleep(0.2)


async def test_hung_read_is_a_plain_timeout(tmp_path):
    """Reads carry no ambiguity: repeating a scrape costs a budget slot, nothing more."""
    async with Rig(tmp_path) as rig:
        rig.executor.hang.clear()
        outcome = await rig.conn.dispatch(
            tool="get_person_profile", args={"linkedin_username": "janeroe"},
            run_id=uuid4(), idempotency_key="read-1", deadline_s=0.5,
        )
        assert outcome.ok is False
        assert outcome.error_kind == "timeout"
        assert outcome.presumed_performed is False, "a read is never 'presumed performed'"
        rig.executor.hang.set()
        await asyncio.sleep(0.2)


async def test_write_that_provably_failed_is_reported_retryable(tmp_path):
    """A classified failure reaches the ledger, so the cloud learns it may retry."""
    from gtmos_bridge.executor import ExecutorError

    async with Rig(tmp_path) as rig:
        rig.executor.raise_exc = ExecutorError("429 slow down", kind="rate_limited")
        outcome = await rig.conn.dispatch(
            tool="connect_with_person", args={"linkedin_username": "janeroe"},
            run_id=uuid4(), idempotency_key="failed-1", deadline_s=5,
        )
        assert outcome.ok is False and outcome.error_kind == "rate_limited"
        assert outcome.presumed_performed is False
        assert rig.ledger.outcome("failed-1") == "not_performed"


async def test_disconnect_mid_dispatch_fails_fast_instead_of_hanging(tmp_path):
    """A dropped bridge must wake its waiters rather than stranding the run."""
    async with Rig(tmp_path) as rig:
        rig.executor.hang.clear()
        task = asyncio.create_task(
            rig.conn.dispatch(
                tool="get_person_profile", args={}, run_id=uuid4(),
                idempotency_key="dropped-1", deadline_s=120,  # would hang for 2min+
            )
        )
        await asyncio.sleep(0.3)
        rig.conn.abandon_inflight()

        with pytest.raises(BridgeUnavailable) as excinfo:
            await asyncio.wait_for(task, timeout=3.0)
        assert excinfo.value.reason == "disconnected"
        rig.executor.hang.set()
