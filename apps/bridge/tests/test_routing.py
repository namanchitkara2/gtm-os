"""Routing between the LinkedIn backend and the web agent.

The two properties that matter: a web read never spends the LinkedIn budget,
and one backend's ill health never refuses the other's work.
"""

from __future__ import annotations

import pytest

from gtmos_bridge.executor import ExecutorError
from gtmos_bridge.routing import RoutingExecutor
from gtmos_schemas.bridge import WEB_TOOLS, WRITE_ACTIONS, BridgeAck, BridgeReject, BridgeResult

from test_tunnel import FakeExecutor, Harness, dispatch


class FakeWeb(FakeExecutor):
    def known_tools(self) -> set[str]:
        return {"web_navigate", "web_extract"}


def routed():
    linkedin, web = FakeExecutor(), FakeWeb()
    return linkedin, web, RoutingExecutor(linkedin, web)


def test_web_tools_are_reads_and_act_is_not_offered():
    assert not WEB_TOOLS & WRITE_ACTIONS
    assert "web_act" not in WEB_TOOLS


def test_colliding_tool_names_are_refused():
    with pytest.raises(ValueError, match="two backends"):
        RoutingExecutor(FakeExecutor(), FakeExecutor())


async def test_calls_route_to_the_owning_backend():
    linkedin, web, r = routed()
    await r.call("web_extract", {"instruction": "x"}, timeout_s=5)
    await r.call("get_person_profile", {}, timeout_s=5)
    assert [c[0] for c in web.calls] == ["web_extract"]
    assert [c[0] for c in linkedin.calls] == ["get_person_profile"]
    with pytest.raises(ExecutorError):
        await r.call("web_act", {}, timeout_s=5)


async def test_web_read_does_not_spend_linkedin_budget(tmp_path):
    _, _, r = routed()
    async with Harness(tmp_path, limit=1, executor=r) as h:
        for _ in range(3):
            await h.send(dispatch(tool="web_extract", instruction="jobs"))
            await h.expect(BridgeAck)
            assert (await h.expect(BridgeResult)).ok
        assert h.budget.used_today() == 0
        # And the one LinkedIn action the budget allows is still there.
        await h.send(dispatch())
        await h.expect(BridgeAck)
        assert (await h.expect(BridgeResult)).ok


async def test_linkedin_checkpoint_does_not_block_web_reads(tmp_path):
    linkedin, _, r = routed()
    async with Harness(tmp_path, executor=r) as h:
        linkedin.authenticated = False
        await h.send(dispatch(tool="web_navigate", url="https://example.com"))
        await h.expect(BridgeAck)
        assert (await h.expect(BridgeResult)).ok
        await h.send(dispatch())
        assert (await h.expect(BridgeReject)).reason == "unauthenticated"


async def test_web_agent_down_does_not_block_linkedin(tmp_path):
    _, web, r = routed()
    async with Harness(tmp_path, executor=r) as h:
        web.browser_ready = False
        await h.send(dispatch(tool="web_extract", instruction="x"))
        assert (await h.expect(BridgeReject)).reason == "browser_unavailable"
        await h.send(dispatch())
        await h.expect(BridgeAck)
        assert (await h.expect(BridgeResult)).ok
