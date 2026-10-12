"""`define_target` and `start_run` as the agent actually calls them.

The parser has its own unit tests. These cover the wiring, which is where the
original bug lived: the parser was never the problem, the *tool* was, because it
built its spec from client-supplied kwargs and persisted whatever came out.

The claim this file exists to assert is the one made in the commit message and
the code comment and nowhere else: **a refused brief is not written to the
database.** A refusal that still leaves a row behind is worse than no refusal at
all, because a later `start_run` can pick that row up and spend the budget on it —
which is exactly the failure being fixed, arriving one call later.

Needs live Postgres; skips loudly without it.
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from gtmos_control.db import Database  # noqa: E402
from gtmos_control.tools import register_tools  # noqa: E402

APP_DSN = os.environ.get("DATABASE_URL", "postgresql://gtmos_app:app@localhost:55432/gtmos")
SYSTEM_DSN = os.environ.get(
    "DATABASE_SYSTEM_URL", "postgresql://gtmos_system:system@localhost:55432/gtmos"
)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


class _CapturingMCP:
    """Stands in for FastMCP, keeping the decorated coroutines callable.

    Registering against the real FastMCP would work too, but reaching back
    through its tool registry to invoke a function couples the test to that
    library's internals. The decorator contract is the only thing being relied on
    here.
    """

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *_args, **_kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _StubExecutor:
    """Records whether a run was ever started. Never touches a bridge."""

    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start(self, **kwargs):
        self.started.append(kwargs)
        return uuid.uuid4()


class _StubRegistry:
    def get(self, _tenant_id):
        return None


@pytest.fixture
async def rig():
    db = Database(APP_DSN, SYSTEM_DSN)
    try:
        await db.connect(min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")

    mcp = _CapturingMCP()
    executor = _StubExecutor()

    async def identity():
        return TENANT, USER

    register_tools(
        mcp, db=db, registry=_StubRegistry(), executor=executor, identity=identity
    )
    try:
        yield mcp.tools, db, executor
    finally:
        async with db.system() as conn:
            await conn.execute("DELETE FROM targets WHERE name LIKE 'integration-test-%'")
        await db.close()


async def _target_count(db: Database) -> int:
    async with db.system() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM targets WHERE name LIKE 'integration-test-%'"
        )


class TestRefusalIsNotPersisted:
    async def test_an_unreadable_brief_writes_no_row(self, rig):
        """The claim under test. A refused target must leave nothing behind."""
        tools, db, _ = rig
        before = await _target_count(db)

        result = await tools["define_target"](
            brief="Find me some good people to talk to next quarter.",
            name="integration-test-unreadable",
        )

        assert result["error"] == "brief_not_understood"
        assert "target_id" not in result, "a refusal must not hand back a usable id"
        assert await _target_count(db) == before, (
            "a refused brief was PERSISTED — a later start_run can pick it up and "
            "spend the LinkedIn budget on it"
        )

    async def test_the_refusal_echoes_the_brief_back(self, rig):
        """So the agent can show the user what was not understood."""
        tools, _, _ = rig
        brief = "vague nonsense with no criteria whatsoever"
        result = await tools["define_target"](brief=brief)
        assert result["brief_was"] == brief

    async def test_the_refusal_is_actionable(self, rig):
        tools, _, _ = rig
        result = await tools["define_target"](brief="aaaa bbbb cccc")
        assert result["needed"]
        assert isinstance(result["how_to_fix"], list) and result["how_to_fix"]
        assert "parsed_so_far" in result

    async def test_an_empty_brief_is_refused(self, rig):
        tools, db, _ = rig
        before = await _target_count(db)
        result = await tools["define_target"](brief="")
        assert "error" in result
        assert await _target_count(db) == before


class TestGoodBriefStillWorks:
    async def test_a_brief_only_call_now_produces_a_real_spec(self, rig):
        """The original bug, inverted: no hints, and still a usable target.

        Before the parser, this exact call shape returned interpreted_as: {}.
        """
        tools, _, _ = rig
        result = await tools["define_target"](
            brief="Find VP Marketing at Series B/C SaaS companies in the US "
                  "with 100-500 employees.",
            name="integration-test-brief-only",
        )
        assert "target_id" in result
        assert result["interpreted_as"] != {}, "THE ORIGINAL BUG"
        assert result["confidence"] > 0.5
        assert result["parser_version"]
        assert result["read_from"], "provenance must say which fields came from where"

    async def test_caller_hints_are_assembled_from_the_tool_kwargs(self, rig):
        """The wiring the unit tests cannot see."""
        tools, _, _ = rig
        result = await tools["define_target"](
            brief="growth people in India",
            name="integration-test-hints",
            titles=["Head of Growth Marketing"],
            headcount_min=50,
        )
        assert result["interpreted_as"]["titles"] == ["Head of Growth Marketing"]
        assert result["interpreted_as"]["headcount_min"] == 50
        assert result["read_from"]["titles"] == "supplied_by_caller"
        assert result["read_from"]["locations"] == "parsed_from_brief"

    async def test_the_brief_is_stored_verbatim(self, rig):
        tools, db, _ = rig
        brief = "Directors of RevOps in London, B2B SaaS, over 200 employees"
        result = await tools["define_target"](brief=brief, name="integration-test-verbatim")
        async with db.system() as conn:
            stored = await conn.fetchval(
                "SELECT brief FROM targets WHERE id = $1", uuid.UUID(result["target_id"])
            )
        assert stored == brief, "the original ask must survive for later correction"


class TestStartRunGate:
    async def test_start_run_refuses_a_legacy_empty_target(self, rig):
        """Rows written before the parser existed carry empty specs.

        This is why the gate is duplicated at the point of spend rather than only
        at the point of creation.
        """
        tools, db, executor = rig
        async with db.scoped(tenant_id=TENANT, user_id=USER) as conn:
            legacy_id = await conn.fetchval(
                "INSERT INTO targets (tenant_id, kind, name, brief, spec, created_by) "
                "VALUES ($1,'icp','integration-test-legacy','old brief','{}',$2) "
                "RETURNING id",
                TENANT, USER,
            )

        result = await tools["start_run"](
            workflow="sales_sourcing", target_id=str(legacy_id), max_people=10
        )

        assert result["error"] == "target_not_executable"
        assert executor.started == [], "THE BUDGET MUST NOT BE SPENT on an empty spec"

    async def test_start_run_refuses_a_headcount_only_target(self, rig):
        """LinkedIn people-search cannot filter on headcount, so this is unfiltered."""
        tools, db, executor = rig
        async with db.scoped(tenant_id=TENANT, user_id=USER) as conn:
            target_id = await conn.fetchval(
                "INSERT INTO targets (tenant_id, kind, name, brief, spec, created_by) "
                "VALUES ($1,'icp','integration-test-hconly','x',"
                "'{\"headcount_min\": 100, \"headcount_max\": 500}',$2) RETURNING id",
                TENANT, USER,
            )
        result = await tools["start_run"](
            workflow="sales_sourcing", target_id=str(target_id), max_people=5
        )
        assert result["error"] == "target_not_executable"
        assert executor.started == []

    async def test_start_run_proceeds_on_a_real_target(self, rig):
        tools, _, executor = rig
        created = await tools["define_target"](
            brief="VP Marketing at SaaS companies in the United States",
            name="integration-test-runnable",
        )
        result = await tools["start_run"](
            workflow="sales_sourcing", target_id=created["target_id"], max_people=3
        )
        assert "run_id" in result, result
        assert len(executor.started) == 1

    async def test_start_run_cannot_reach_another_tenants_target(self, rig):
        """IDOR at the tool layer, not just the SQL layer."""
        tools, db, executor = rig
        other_tenant = uuid.UUID("99999999-9999-9999-9999-999999999999")
        async with db.system() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, slug) VALUES ($1,'Other','other-integ') "
                "ON CONFLICT (id) DO NOTHING", other_tenant,
            )
            foreign_id = await conn.fetchval(
                "INSERT INTO targets (tenant_id, kind, name, brief, spec, created_by) "
                "VALUES ($1,'icp','integration-test-foreign','x',"
                "'{\"titles\": [\"CEO\"]}',NULL) RETURNING id",
                other_tenant,
            )
        try:
            result = await tools["start_run"](
                workflow="sales_sourcing", target_id=str(foreign_id), max_people=1
            )
            assert "not found in this workspace" in result["error"]
            assert executor.started == []
        finally:
            async with db.system() as conn:
                await conn.execute("DELETE FROM targets WHERE id = $1", foreign_id)
                await conn.execute("DELETE FROM tenants WHERE id = $1", other_tenant)
