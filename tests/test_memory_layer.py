"""The memory layer against a real Postgres, because the guarantees are in SQL.

These tests need the local database (`./run-local.sh` or `docker start gtmos-pg`)
and skip cleanly without it. That is deliberate: the three things being checked
here — a CHECK constraint, an append-only supersede, and row-level security —
cannot be verified against a mock, and a mock that pretended to would be worse
than no test at all.

Every test runs inside a transaction that is rolled back, so the developer's
local workspace is not filled with fixtures.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from gtmos_control import memory  # noqa: E402

# gtmos_app, not the owner. Connecting as a superuser silently bypasses RLS, so
# a suite that did would prove the policies exist without ever proving they
# apply — and would see every other tenant's rows while claiming to be scoped.
DSN = os.environ.get(
    "GTMOS_TEST_DSN", "postgresql://gtmos_app:app@localhost:55432/gtmos"
)


class Scoped:
    """A connection that also knows which tenant it is scoped to.

    A wrapper rather than an attribute on the connection itself, because
    ``asyncpg.Connection`` uses ``__slots__`` and refuses new attributes — which
    is a reasonable thing for a hot-path object to do.
    """

    def __init__(self, conn, tenant_id: uuid.UUID) -> None:
        self._conn = conn
        self._tenant_id = tenant_id

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
async def conn():
    """A tenant-scoped connection inside a transaction that never commits."""
    try:
        connection = await asyncpg.connect(DSN, timeout=3)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"local postgres not reachable: {exc}")

    tx = connection.transaction()
    await tx.start()
    tenant_id = await connection.fetchval(
        "INSERT INTO tenants (name, slug) VALUES ('memtest', $1) RETURNING id",
        f"memtest-{uuid.uuid4().hex[:8]}",
    )
    # Mirrors Database.scoped(): RLS keys on this setting, so a test that forgets
    # it would exercise a different code path than production.
    await connection.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
    try:
        yield Scoped(connection, tenant_id)
    finally:
        await tx.rollback()
        await connection.close()


async def _person(conn, name: str = "Test Person") -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO people (tenant_id, dedupe_key, full_name, doc) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        conn._tenant_id, f"k-{uuid.uuid4().hex[:8]}", name, json.dumps({"full_name": name}),
    )


# ------------------------------------------------------------ provenance --


@pytest.mark.asyncio
async def test_new_observation_supersedes_the_old_one_without_deleting_it(conn):
    """Age is the product. Overwriting a fact destroys the ability to date it."""
    pid = await _person(conn)
    await memory.record_fact(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        field="title", value="Head of Growth", source="linkedin", confidence=0.95,
    )
    await memory.record_fact(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        field="title", value="VP Growth", source="linkedin", confidence=0.95,
    )

    facts = await memory.current_facts(conn, "person", pid)
    assert [f["value"] for f in facts] == ["VP Growth"]

    history = await conn.fetchval(
        "SELECT count(*) FROM field_provenance WHERE entity_id=$1 AND field='title'", pid
    )
    assert history == 2, "the previous observation must survive; the differ reads it"


@pytest.mark.asyncio
async def test_every_recalled_fact_carries_its_age(conn):
    pid = await _person(conn)
    await memory.record_fact(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        field="email", value="a@b.com", source="apollo", confidence=0.8,
    )
    facts = await memory.current_facts(conn, "person", pid)
    assert facts[0]["age_days"] is not None
    assert facts[0]["source"] == "apollo"


# -------------------------------------------------------------- decisions --


@pytest.mark.asyncio
async def test_model_cannot_record_a_verdict_without_evidence(conn):
    """ADR-007, enforced by the database rather than by a prompt."""
    pid = await _person(conn)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await memory.record_decision(
            conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
            verdict="qualified", reason="seems good", actor_type="model", actor_id="gpt",
        )


@pytest.mark.asyncio
async def test_human_may_decide_without_evidence(conn):
    """Because in that case the human is the evidence."""
    pid = await _person(conn)
    decision_id = await memory.record_decision(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        verdict="disqualified", reason="spoke to them, wrong stage", actor_type="human",
        actor_id="naman",
    )
    assert decision_id is not None


@pytest.mark.asyncio
async def test_recall_returns_decisions_interactions_and_spend(conn):
    pid = await _person(conn)
    evidence_id = await conn.fetchval(
        "INSERT INTO evidence (tenant_id, entity_type, entity_id, kind, body, source) "
        "VALUES ($1,'person',$2,'profile_fact','Headline says Head of Growth','linkedin') "
        "RETURNING id",
        conn._tenant_id, pid,
    )
    await memory.record_decision(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        verdict="qualified", reason="runs growth at a 200-person marketplace",
        actor_type="model", actor_id="claude", evidence_ids=[evidence_id],
    )
    await conn.execute(
        "INSERT INTO interactions (tenant_id, person_id, direction, channel, body, objection) "
        "VALUES ($1,$2,'inbound','linkedin_dm','not now, just signed with a competitor','incumbent')",
        conn._tenant_id, pid,
    )

    recalled = await memory.recall(conn, "person", pid)
    assert recalled["decisions"][0]["verdict"] == "qualified"
    assert recalled["decisions"][0]["evidence_count"] == 1
    assert recalled["interactions"][0]["objection"] == "incumbent"
    assert recalled["spend"]["usd_spent"] == 0.0


# ------------------------------------------------------- change detection --


@pytest.mark.asyncio
async def test_first_snapshot_reports_no_change(conn):
    pid = await _person(conn)
    result = await memory.snapshot(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        doc={"title": "Head of Growth", "company": "Acme"}, source="linkedin",
    )
    assert result["first_snapshot"] is True and result["changes"] == []


@pytest.mark.asyncio
async def test_identical_reread_is_not_a_change(conn):
    """The digest short-circuit: most re-reads find nothing, and must say so cheaply."""
    pid = await _person(conn)
    doc = {"title": "Head of Growth", "company": "Acme"}
    await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person",
                          entity_id=pid, doc=doc, source="linkedin")
    again = await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person",
                                  entity_id=pid, doc=dict(doc), source="linkedin")
    assert again["changes"] == []


@pytest.mark.asyncio
async def test_company_move_is_detected_with_before_and_after(conn):
    pid = await _person(conn)
    await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
                          doc={"title": "Head of Growth", "company": "Acme"}, source="linkedin")
    changed = await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person",
                                    entity_id=pid,
                                    doc={"title": "VP Growth", "company": "Stripe"},
                                    source="linkedin")
    fields = {c["field"]: (c["from"], c["to"]) for c in changed["changes"]}
    assert fields["company"] == ("Acme", "Stripe")
    assert fields["title"] == ("Head of Growth", "VP Growth")


@pytest.mark.asyncio
async def test_a_detected_change_raises_a_signal_by_itself(conn):
    """A diff nobody turns into a signal is a diff nobody sees."""
    pid = await _person(conn, "Job Changer")
    await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
                          doc={"title": "Head of Growth", "company": "Acme"}, source="linkedin")
    result = await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person",
                                   entity_id=pid,
                                   doc={"title": "VP Growth", "company": "Stripe"},
                                   source="linkedin")

    assert set(result["signals_raised"]) == {"company_change", "title_change"}
    surfaced = await memory.whats_changed(conn, days=30, limit=10)
    assert any(c["who"] == "Job Changer" and c["signal"] == "company_change" for c in surfaced)


@pytest.mark.asyncio
async def test_an_unchanged_reread_raises_nothing(conn):
    pid = await _person(conn)
    doc = {"title": "Head of Growth", "company": "Acme"}
    await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person",
                          entity_id=pid, doc=doc, source="linkedin")
    again = await memory.snapshot(conn, tenant_id=conn._tenant_id, entity_type="person",
                                  entity_id=pid, doc=dict(doc), source="linkedin")
    assert again["changes"] == []
    assert await memory.whats_changed(conn, days=30, limit=10) == []


@pytest.mark.asyncio
async def test_signals_rank_company_change_above_cosmetic_change(conn):
    """A job move is the warmest signal in outbound; a new headline is not."""
    mover = await _person(conn, "Mover")
    tweaker = await _person(conn, "Tweaker")
    await memory.raise_signal(conn, tenant_id=conn._tenant_id, entity_type="person",
                              entity_id=tweaker, signal_type="headline_change",
                              summary="reworded headline", source="snapshot_diff")
    await memory.raise_signal(conn, tenant_id=conn._tenant_id, entity_type="person",
                              entity_id=mover, signal_type="company_change",
                              summary="Acme -> Stripe", source="snapshot_diff")

    changes = await memory.whats_changed(conn, days=30, limit=10)
    assert changes[0]["who"] == "Mover"
    assert changes[0]["strength"] > changes[-1]["strength"]


# ----------------------------------------------------------- rediscovery --


@pytest.mark.asyncio
async def test_rediscovery_surfaces_a_deferral_and_costs_nothing(conn):
    pid = await _person(conn, "Deferred Person")
    await memory.record_decision(
        conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
        verdict="deferred", reason="good fit, no budget this quarter", actor_type="human",
    )
    found = await memory.rediscover(conn, target_id=None, limit=10)
    assert any(p["name"] == "Deferred Person" for p in found)
    assert all(p["cost_to_surface_usd"] == 0.0 for p in found)


@pytest.mark.asyncio
async def test_a_target_narrows_rediscovery_instead_of_being_ignored(conn):
    """The parameter has to do something, or it should not be in the signature."""
    matching = await _person(conn, "Growth Person")
    unrelated = await _person(conn, "Warehouse Person")
    await conn.execute(
        "UPDATE people SET headline='VP Growth at a marketplace' WHERE id=$1", matching
    )
    await conn.execute(
        "UPDATE people SET headline='Warehouse operations lead' WHERE id=$1", unrelated
    )
    for pid in (matching, unrelated):
        await memory.record_decision(
            conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
            verdict="deferred", reason="no budget then", actor_type="human",
        )
    target_id = await conn.fetchval(
        "INSERT INTO targets (tenant_id, kind, name, brief, spec) "
        "VALUES ($1,'icp','Growth leaders','growth leaders at marketplaces',$2) RETURNING id",
        conn._tenant_id, json.dumps({"titles": ["VP Growth", "Head of Growth"]}),
    )

    names = {p["name"] for p in await memory.rediscover(conn, target_id=target_id, limit=20)}
    assert "Growth Person" in names
    assert "Warehouse Person" not in names

    everyone = {p["name"] for p in await memory.rediscover(conn, target_id=None, limit=20)}
    assert {"Growth Person", "Warehouse Person"} <= everyone


@pytest.mark.asyncio
async def test_disqualified_stays_buried_until_something_changes(conn):
    """A considered 'no' should not resurface as noise. A job move earns a second look."""
    quiet = await _person(conn, "Still No")
    moved = await _person(conn, "Moved On")
    for pid in (quiet, moved):
        await memory.record_decision(
            conn, tenant_id=conn._tenant_id, entity_type="person", entity_id=pid,
            verdict="disqualified", reason="wrong function", actor_type="human",
        )
    await memory.raise_signal(conn, tenant_id=conn._tenant_id, entity_type="person",
                              entity_id=moved, signal_type="company_change",
                              summary="now at a target account", source="snapshot_diff")

    names = {p["name"] for p in await memory.rediscover(conn, target_id=None, limit=20)}
    assert "Moved On" in names
    assert "Still No" not in names


# ------------------------------------------------------------- approvals --


@pytest.mark.asyncio
async def test_an_entity_can_be_queued_again_after_a_refresh_completes(conn):
    """The queue drains and refills; a plain UNIQUE would lock each entity out forever."""
    pid = await _person(conn)
    await conn.execute(
        "INSERT INTO refresh_queue (tenant_id, entity_type, entity_id, reason) "
        "VALUES ($1,'person',$2,'first look')",
        conn._tenant_id, pid,
    )
    await conn.execute(
        "UPDATE refresh_queue SET completed_at=now() WHERE entity_id=$1", pid
    )
    await conn.execute(
        "INSERT INTO refresh_queue (tenant_id, entity_type, entity_id, reason) "
        "VALUES ($1,'person',$2,'changed jobs since')",
        conn._tenant_id, pid,
    )
    pending = await conn.fetchval(
        "SELECT count(*) FROM refresh_queue WHERE entity_id=$1 AND completed_at IS NULL", pid
    )
    assert pending == 1


@pytest.mark.asyncio
async def test_two_pending_refreshes_for_one_entity_are_refused(conn):
    pid = await _person(conn)
    await conn.execute(
        "INSERT INTO refresh_queue (tenant_id, entity_type, entity_id, reason) "
        "VALUES ($1,'person',$2,'first')",
        conn._tenant_id, pid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await conn.execute(
            "INSERT INTO refresh_queue (tenant_id, entity_type, entity_id, reason) "
            "VALUES ($1,'person',$2,'duplicate')",
            conn._tenant_id, pid,
        )


@pytest.mark.asyncio
async def test_unknown_action_class_requires_approval(conn):
    """The safe reading of 'nobody has thought about this' is not 'send it'."""
    assert await memory.approval_mode(conn, "something_new") == "require_approval"


@pytest.mark.asyncio
async def test_an_approval_can_only_be_decided_once(conn):
    pid = await _person(conn)
    approval_id = await memory.request_approval(
        conn, tenant_id=conn._tenant_id, action_class="outbound_message",
        subject_type="draft", subject_id=pid, payload={"body": "hi"},
    )
    assert await memory.decide_approval(
        conn, approval_id=approval_id, approved=True, user_id=None, reason="looks fine"
    ) is True
    assert await memory.decide_approval(
        conn, approval_id=approval_id, approved=False, user_id=None, reason="changed my mind"
    ) is False, "a decided approval must not be silently rewritten"


@pytest.mark.asyncio
async def test_default_policy_holds_outbound_and_lets_enrichment_run(conn):
    """Migration 0002 seeds this; it is the product's default posture."""
    assert await memory.approval_mode(conn, "outbound_message") == "require_approval"
    assert await memory.approval_mode(conn, "enrichment") == "auto"


# ----------------------------------------------------------------- claims --


@pytest.mark.asyncio
async def test_a_claim_cannot_exist_without_evidence(conn):
    """'Never fabricate personalisation' as a foreign key, not a prompt line."""
    pid = await _person(conn)
    draft_id = await conn.fetchval(
        "INSERT INTO message_drafts (tenant_id, person_id, channel, body, produced_by) "
        "VALUES ($1,$2,'linkedin_dm','Saw you moved to Stripe','claude') RETURNING id",
        conn._tenant_id, pid,
    )
    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await conn.execute(
            "INSERT INTO draft_claims (tenant_id, draft_id, claim, evidence_id) "
            "VALUES ($1,$2,'moved to Stripe', NULL)",
            conn._tenant_id, draft_id,
        )


# ---------------------------------------------------------------- spend --


@pytest.mark.asyncio
async def test_spend_report_separates_spend_from_savings(conn):
    pid = await _person(conn)
    await conn.executemany(
        "INSERT INTO enrichment_ledger "
        "(tenant_id, entity_type, entity_id, field, provider, outcome, cost_usd) "
        "VALUES ($1,'person',$2,$3,$4,$5,$6)",
        [
            (conn._tenant_id, pid, "email", "apollo", "hit", 0.04),
            (conn._tenant_id, pid, "phone", "premium", "miss", 0.24),
            (conn._tenant_id, pid, "title", "cache", "skipped_cache", 0.02),
        ],
    )
    report = await memory.spend_report(conn, days=30)
    assert report["usd_spent"] == pytest.approx(0.28)
    assert report["usd_saved_by_cache"] == pytest.approx(0.02)
