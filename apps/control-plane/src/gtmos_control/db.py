"""Postgres access, with tenant scope set on every transaction.

The schema's row-level security keys on ``app.tenant_id``. That is only a
guarantee if something actually sets it, and only a *safe* guarantee if
forgetting to set it fails closed — which it does: an unset variable matches no
row, so a query that skips the scope returns nothing instead of everything.

``SET LOCAL`` rather than ``SET`` is load-bearing. Connections are pooled and
reused across tenants; a session-scoped setting would leak the previous
occupant's tenant to the next one, which is the exact failure RLS exists to
prevent.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import asyncpg


class Database:
    """Two pools, two roles, on purpose.

    ``app`` is the RLS-enforced role that every tenant-scoped query uses. But
    authentication cannot use it: looking up which tenant owns a bridge token is
    a chicken-and-egg — RLS needs the tenant, and the tenant is what the lookup
    is for. Under the app role such a query returns nothing and pairing silently
    breaks.

    So a second role bypasses RLS, and its surface is kept deliberately tiny:
    token and pairing-code lookups, and the tenants/users tables that sit
    outside tenant scope by definition. Every use goes through :meth:`system`,
    which is named to make reaching for it a visible decision in review rather
    than a convenience someone reaches for to fix a missing-rows bug.
    """

    def __init__(self, dsn: str, system_dsn: str | None = None) -> None:
        self._dsn = dsn
        self._system_dsn = system_dsn or dsn
        self._pool: asyncpg.Pool | None = None
        self._system_pool: asyncpg.Pool | None = None

    async def connect(self, *, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=min_size, max_size=max_size)
        self._system_pool = await asyncpg.create_pool(
            self._system_dsn, min_size=1, max_size=max(2, max_size // 4)
        )

    async def close(self) -> None:
        for pool in (self._pool, self._system_pool):
            if pool is not None:
                await pool.close()
        self._pool = self._system_pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database not connected")
        return self._pool

    @contextlib.asynccontextmanager
    async def scoped(
        self, *, tenant_id: UUID | None = None, user_id: UUID | None = None
    ) -> AsyncIterator[asyncpg.Connection]:
        """A transaction with RLS scope applied, cleared when it ends."""
        async with self.pool.acquire() as conn, conn.transaction():
            if tenant_id is not None:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
            if user_id is not None:
                await conn.execute("SELECT set_config('app.user_id', $1, true)", str(user_id))
            yield conn

    @contextlib.asynccontextmanager
    async def system(self) -> AsyncIterator[asyncpg.Connection]:
        """RLS-bypassing access, for authentication and pairing only.

        Legitimate uses: resolving a bridge bearer token to its tenant,
        redeeming a pairing code, and the tenants/users tables that are not
        tenant-scoped. Anything that handles a customer's *data* belongs in
        :meth:`scoped` — if a query here touches people, runs, or targets,
        that is a bug, not a shortcut.
        """
        if self._system_pool is None:
            raise RuntimeError("database not connected")
        async with self._system_pool.acquire() as conn, conn.transaction():
            yield conn


async def fetch_one(conn: asyncpg.Connection, sql: str, *args: Any) -> dict[str, Any] | None:
    row = await conn.fetchrow(sql, *args)
    return dict(row) if row else None


async def fetch_all(conn: asyncpg.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(sql, *args)]
