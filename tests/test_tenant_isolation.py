"""Can tenant A reach tenant B's data? Proven against live Postgres, not asserted.

Until now the RLS policies in ``0001_init.sql`` and ``0002_memory.sql`` were
untested. A policy that is present but wrong looks exactly like a policy that is
right, and the failure is silent and total, so these tests exist to *attack* the
boundary rather than to demonstrate the happy path: every test here plays tenant A
deliberately passing tenant B's identifiers.

The three defences being checked are separable, and all three must hold:

1. **RLS itself.** The ``gtmos_app`` role has ``rolbypassrls = false``, so a policy
   filters every statement it issues.
2. **Fail-closed on a missing scope.** An unset ``app.tenant_id`` matches no row, so
   a query that forgets to set the scope returns nothing rather than everything.
   This is the property that makes a forgotten ``db.scoped()`` a bug that shows up
   as missing data instead of as a breach.
3. **``SET LOCAL`` rather than ``SET``.** Connections are pooled across tenants; a
   session-scoped setting would leak the previous occupant's scope to the next
   caller.

Skipped, loudly, when Postgres is not reachable — never silently passed, because a
security test that quietly does nothing is worse than no test at all.
"""

from __future__ import annotations

import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from gtmos_control.db import Database  # noqa: E402

APP_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://gtmos_app:app@localhost:55432/gtmos"
)
SYSTEM_DSN = os.environ.get(
    "DATABASE_SYSTEM_URL", "postgresql://gtmos_system:system@localhost:55432/gtmos"
)

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
TENANT_B = uuid.UUID("99999999-9999-9999-9999-999999999999")
USER_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


@pytest.fixture
async def db():
    """Two tenants, each with one target and one person, then torn down."""
    database = Database(APP_DSN, SYSTEM_DSN)
    try:
        await database.connect(min_size=1, max_size=4)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {APP_DSN}: {exc}")

    async with database.system() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, slug) "
            "VALUES ($1,'Isolation A','isolation-a'),($2,'Isolation B','isolation-b') "
            "ON CONFLICT (id) DO NOTHING",
            TENANT_A, TENANT_B,
        )
        # Users are not tenant-scoped: membership lives in `tenant_members`, so one
        # person can belong to several workspaces. That is the right shape, and it
        # is also why isolation cannot be enforced by a column on `users` — it has
        # to be the row-level policy these tests attack.
        for user_id, email, sub in (
            (USER_A, "isolation-a@example.com", "sub-isolation-a"),
            (USER_B, "isolation-b@example.com", "sub-isolation-b"),
        ):
            await conn.execute(
                "INSERT INTO users (id, email, cognito_sub) VALUES ($1,$2,$3) "
                "ON CONFLICT (id) DO NOTHING",
                user_id, email, sub,
            )
        for user_id, tenant_id in ((USER_A, TENANT_A), (USER_B, TENANT_B)):
            await conn.execute(
                "INSERT INTO tenant_members (tenant_id, user_id, role) "
                "VALUES ($1,$2,'owner') ON CONFLICT DO NOTHING",
                tenant_id, user_id,
            )
    try:
        yield database
    finally:
        async with database.system() as conn:
            # Only the rows these tests created; tenant A is the shared dev
            # workspace and its real data must survive.
            await conn.execute("DELETE FROM targets WHERE name LIKE 'isolation-test-%'")
            await conn.execute("DELETE FROM people WHERE full_name LIKE 'isolation-test-%'")
            await conn.execute("DELETE FROM tenant_members WHERE tenant_id = $1", TENANT_B)
            await conn.execute("DELETE FROM users WHERE id = $1", USER_B)
            await conn.execute("DELETE FROM targets WHERE tenant_id = $1", TENANT_B)
            await conn.execute("DELETE FROM people WHERE tenant_id = $1", TENANT_B)
            await conn.execute("DELETE FROM tenants WHERE id = $1", TENANT_B)
        await database.close()


async def _make_target(db: Database, tenant_id: uuid.UUID, user_id: uuid.UUID, label: str):
    async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
        return await conn.fetchval(
            "INSERT INTO targets (tenant_id, kind, name, brief, spec, created_by) "
            "VALUES ($1,'icp',$2,'brief','{}',$3) RETURNING id",
            tenant_id, f"isolation-test-{label}", user_id,
        )


# ------------------------------------------------------------------- reads --

class TestCrossTenantReads:
    async def test_a_cannot_select_bs_target_by_id(self, db):
        b_target = await _make_target(db, TENANT_B, USER_B, "b-target")

        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            row = await conn.fetchrow("SELECT * FROM targets WHERE id = $1", b_target)
        assert row is None, "tenant A read tenant B's target by id — RLS is not working"

    async def test_a_cannot_widen_the_query_to_see_everything(self, db):
        """An unfiltered SELECT must still return only A's own rows."""
        await _make_target(db, TENANT_B, USER_B, "b-wide")
        await _make_target(db, TENANT_A, USER_A, "a-wide")

        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            rows = await conn.fetch("SELECT tenant_id FROM targets")
        assert rows, "A should see its own rows"
        assert {r["tenant_id"] for r in rows} == {TENANT_A}

    async def test_a_cannot_forge_the_tenant_id_in_a_where_clause(self, db):
        """Naming B's tenant explicitly does not grant access — the policy ANDs."""
        await _make_target(db, TENANT_B, USER_B, "b-forge")

        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            rows = await conn.fetch("SELECT * FROM targets WHERE tenant_id = $1", TENANT_B)
        assert rows == []

    async def test_count_leaks_nothing(self, db):
        """Aggregates are filtered too — a count is an information leak."""
        await _make_target(db, TENANT_B, USER_B, "b-count")
        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            visible = await conn.fetchval(
                "SELECT count(*) FROM targets WHERE name LIKE 'isolation-test-b-count%'"
            )
        assert visible == 0


# ------------------------------------------------------------------ writes --

class TestCrossTenantWrites:
    async def test_a_cannot_update_bs_target(self, db):
        b_target = await _make_target(db, TENANT_B, USER_B, "b-update")

        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            status = await conn.execute(
                "UPDATE targets SET name = 'pwned' WHERE id = $1", b_target
            )
        assert status.endswith("0"), f"UPDATE affected rows it should not see: {status}"

        async with db.system() as conn:
            name = await conn.fetchval("SELECT name FROM targets WHERE id = $1", b_target)
        assert name == "isolation-test-b-update", "tenant B's row was modified"

    async def test_a_cannot_delete_bs_target(self, db):
        b_target = await _make_target(db, TENANT_B, USER_B, "b-delete")

        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            status = await conn.execute("DELETE FROM targets WHERE id = $1", b_target)
        assert status.endswith("0")

        async with db.system() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM targets WHERE id = $1", b_target
            ) == 1, "tenant B's row was deleted by tenant A"

    async def test_a_cannot_insert_a_row_owned_by_b(self, db):
        """Writing into another tenant is how you plant data, not just read it.

        The WITH CHECK half of the policy is what stops this, and it is a
        different clause from the USING half that filters reads — a schema can
        easily have one and not the other.
        """
        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO targets (tenant_id, kind, name, brief, spec, created_by) "
                    "VALUES ($1,'icp','isolation-test-planted','x','{}',$2)",
                    TENANT_B, USER_A,
                )

    async def test_a_cannot_reassign_its_own_row_to_b(self, db):
        """Exfiltration by ownership transfer."""
        a_target = await _make_target(db, TENANT_A, USER_A, "a-reassign")
        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "UPDATE targets SET tenant_id = $1 WHERE id = $2", TENANT_B, a_target
                )


# ---------------------------------------------------------- fail-closed --

class TestFailClosed:
    async def test_no_scope_returns_nothing_not_everything(self, db):
        """The property that makes a forgotten db.scoped() safe.

        An unset ``app.tenant_id`` matches no row. If this inverted, every query
        that forgot its scope would silently serve the whole platform.
        """
        await _make_target(db, TENANT_A, USER_A, "a-noscope")
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM targets")
        assert rows == [], "an unscoped query saw rows — RLS is failing OPEN"

    async def test_scope_does_not_leak_between_pooled_connections(self, db):
        """SET LOCAL, not SET. Connections are reused across tenants.

        This is the test that would catch someone 'fixing' a missing-rows bug by
        changing SET LOCAL to SET — which works, and quietly leaks the previous
        caller's tenant to the next one.
        """
        await _make_target(db, TENANT_B, USER_B, "b-leak")

        async with db.scoped(tenant_id=TENANT_B, user_id=USER_B) as conn:
            assert await conn.fetch("SELECT 1 FROM targets") is not None

        # Drain the pool so the same physical connection is very likely reused.
        for _ in range(6):
            async with db.pool.acquire() as conn:
                leaked = await conn.fetchval("SELECT current_setting('app.tenant_id', true)")
                assert leaked in (None, ""), f"tenant scope survived the transaction: {leaked!r}"

    async def test_the_app_role_cannot_bypass_rls(self, db):
        """If this role ever gains BYPASSRLS, every test above becomes decorative."""
        async with db.pool.acquire() as conn:
            bypasses = await conn.fetchval(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        assert bypasses is False, "the application role can bypass RLS"

    async def test_the_app_role_cannot_disable_rls(self, db):
        """Table ownership would let the app turn the policy off entirely."""
        async with db.pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("ALTER TABLE targets DISABLE ROW LEVEL SECURITY")


# ------------------------------------------------- coverage of the schema --

class TestPolicyCoverage:
    #: Every table holding customer data. A new tenant-scoped table that is not
    #: listed here is the gap this test exists to catch.
    TENANT_TABLES = (
        "targets", "runs", "run_steps", "run_people", "people", "companies",
        "evidence", "field_provenance", "interactions", "qualification_decisions",
        "research_artifacts", "entity_snapshots", "signals", "enrichment_ledger",
        "approvals", "approval_policies", "action_outcomes", "message_drafts",
        "draft_claims", "provider_keys", "bridges", "usage_daily",
    )

    async def test_every_tenant_table_has_rls_enabled(self, db):
        async with db.system() as conn:
            rows = await conn.fetch(
                "SELECT tablename, rowsecurity FROM pg_tables "
                "WHERE schemaname='public' AND tablename = ANY($1)",
                list(self.TENANT_TABLES),
            )
        found = {r["tablename"]: r["rowsecurity"] for r in rows}
        missing = [t for t in self.TENANT_TABLES if t not in found]
        assert not missing, f"tables absent from the schema: {missing}"
        unprotected = [t for t, on in found.items() if not on]
        assert not unprotected, f"RLS is OFF for: {unprotected}"

    async def test_every_tenant_table_has_a_policy_that_reads_the_scope(self, db):
        """RLS enabled with no policy denies everything; with a policy that does
        not reference app.tenant_id it protects nothing. Both are checked."""
        async with db.system() as conn:
            rows = await conn.fetch(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE schemaname='public' AND tablename = ANY($1)",
                list(self.TENANT_TABLES),
            )
        by_table: dict[str, list] = {}
        for row in rows:
            by_table.setdefault(row["tablename"], []).append(row)

        for table in self.TENANT_TABLES:
            policies = by_table.get(table, [])
            assert policies, f"{table} has RLS on but no policy"
            clauses = " ".join(
                (p["qual"] or "") + " " + (p["with_check"] or "") for p in policies
            )
            assert "app.tenant_id" in clauses, (
                f"{table}'s policy does not reference app.tenant_id: {clauses!r}"
            )

    async def test_provider_keys_are_tenant_scoped(self, db):
        """BYOK credentials. The one table where a leak is another tenant's money."""
        async with db.scoped(tenant_id=TENANT_A, user_id=USER_A) as conn:
            rows = await conn.fetch("SELECT tenant_id FROM provider_keys")
        assert all(r["tenant_id"] == TENANT_A for r in rows)
