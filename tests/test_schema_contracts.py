"""Contracts that span two languages and would otherwise drift silently.

Each test here guards a fact that is stated twice — once in Python, once in SQL
— with nothing but discipline keeping the two in agreement. Discipline loses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gtmos_schemas.bridge import WRITE_ACTIONS

MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations" / "0001_init.sql"


@pytest.fixture(scope="module")
def migration_sql() -> str:
    return MIGRATION.read_text()


def test_write_actions_match_ledger_check_constraint(migration_sql: str) -> None:
    """Every LinkedIn write must be storable in the ledger that prevents repeats.

    ``WRITE_ACTIONS`` decides which dispatches get idempotency treatment;
    ``action_ledger.action``'s CHECK decides which can be recorded at all. If a
    Phase 2 action (follow, endorse, InMail) is added to one and not the other,
    the failure is silent and severe: either the write is treated as a read and
    can be sent twice, or the ledger insert raises and the send is blocked.
    """
    match = re.search(
        r"action\s+TEXT NOT NULL CHECK \(action IN \(([^)]*)\)\)", migration_sql
    )
    assert match, "action_ledger.action CHECK constraint not found — did the schema move?"

    sql_actions = set(re.findall(r"'([^']+)'", match.group(1)))
    assert sql_actions == set(WRITE_ACTIONS), (
        "WRITE_ACTIONS and the action_ledger CHECK constraint disagree.\n"
        f"  only in Python: {sorted(set(WRITE_ACTIONS) - sql_actions)}\n"
        f"  only in SQL:    {sorted(sql_actions - set(WRITE_ACTIONS))}\n"
        "Add the action to BOTH, and write a migration for the constraint."
    )


def test_run_status_values_match(migration_sql: str) -> None:
    """The API's status vocabulary must be storable."""
    from typing import get_args

    from gtmos_schemas.runs import RunStatus

    match = re.search(r"status\s+TEXT NOT NULL DEFAULT 'queued' CHECK \(status IN\s*\(([^)]*)\)",
                      migration_sql, re.S)
    assert match, "runs.status CHECK constraint not found"
    assert set(re.findall(r"'([^']+)'", match.group(1))) == set(get_args(RunStatus))


def test_step_status_values_match(migration_sql: str) -> None:
    from typing import get_args

    from gtmos_schemas.runs import StepStatus

    match = re.search(r"status\s+TEXT NOT NULL CHECK \(status IN \(([^)]*)\)\)", migration_sql)
    assert match, "run_steps.status CHECK constraint not found"
    assert set(re.findall(r"'([^']+)'", match.group(1))) == set(get_args(StepStatus))


def test_every_tenant_table_has_rls(migration_sql: str) -> None:
    """A tenant-scoped table left out of the RLS loop is a silent cross-tenant leak."""
    tables_with_tenant = set(
        re.findall(r"CREATE TABLE (\w+) \((?:[^;]*?)tenant_id\s+UUID NOT NULL", migration_sql)
    )
    rls_block = re.search(r"FOREACH t IN ARRAY ARRAY\[(.*?)\]", migration_sql, re.S)
    assert rls_block, "RLS loop not found"
    protected = set(re.findall(r"'([^']+)'", rls_block.group(1)))

    # A table may also be protected by its own hand-written policy — tenant_members
    # is, because it must be readable before a tenant is known. Either counts;
    # having no policy at all does not.
    protected |= set(re.findall(r"CREATE POLICY \w+ ON (\w+)", migration_sql))

    unprotected = tables_with_tenant - protected
    assert not unprotected, (
        f"tables carry tenant_id but are not in the RLS loop: {sorted(unprotected)}"
    )
