"""Creating runs, executing them off the request path, and persisting the trace.

An MCP tool call must never block on a run. Sourcing 25 people is 26 LinkedIn
actions taken strictly one at a time; at even five seconds each that is minutes,
and every MCP client would time out. So ``start_run`` returns a ``run_id``
immediately and the work proceeds in the background, exactly as the deployed
version will with SQS and a separate worker — the seam is
:class:`RunExecutor`, and only its innards change.

The step trace is written as the run goes, not at the end. A run that dies still
leaves a readable trace up to the point it died, which is the only version of a
trace that is any use when something has gone wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from gtmos_schemas import ICP, Person
from gtmos_control.bridges import BridgeRegistry, BridgeUnavailable
from gtmos_control.db import Database
from gtmos_control.workflows import WORKFLOW_NODES, RunContext, StepCost

logger = logging.getLogger(__name__)


class RunExecutor:
    def __init__(self, db: Database, registry: BridgeRegistry) -> None:
        self._db = db
        self._registry = registry
        self._tasks: dict[UUID, asyncio.Task] = {}

    # ------------------------------------------------------------ lifecycle --

    async def start(
        self, *, tenant_id: UUID, user_id: UUID, workflow: str, icp: ICP,
        target_id: UUID | None, limits: dict[str, int],
    ) -> UUID:
        if workflow not in WORKFLOW_NODES:
            raise ValueError(f"unknown workflow {workflow!r}")
        run_id = uuid4()
        async with self._db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            await conn.execute(
                "INSERT INTO runs (id, tenant_id, user_id, workflow, target_id, status, limits) "
                "VALUES ($1,$2,$3,$4,$5,'queued',$6)",
                run_id, tenant_id, user_id, workflow, target_id, json.dumps(limits),
            )
        task = asyncio.create_task(self._execute(run_id, tenant_id, user_id, workflow, icp, limits))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return run_id

    async def cancel(self, run_id: UUID, tenant_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
        async with self._db.scoped(tenant_id=tenant_id) as conn:
            updated = await conn.execute(
                "UPDATE runs SET status='cancelled', ended_at=now() "
                "WHERE id=$1 AND status NOT IN ('succeeded','failed','cancelled')",
                run_id,
            )
        return updated.endswith("1")

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -------------------------------------------------------------- execute --

    async def _execute(
        self, run_id: UUID, tenant_id: UUID, user_id: UUID, workflow: str,
        icp: ICP, limits: dict[str, int],
    ) -> None:
        await self._set_status(tenant_id, run_id, "running", started=True)
        ctx = RunContext(
            run_id=run_id, tenant_id=tenant_id, icp=icp, limits=limits,
            dispatch=self._make_dispatch(tenant_id, run_id),
            cache_lookup=self._make_cache_lookup(tenant_id),
            cache_store=self._make_cache_store(tenant_id, run_id),
        )
        sequence = 0
        try:
            for name, node in WORKFLOW_NODES[workflow]:
                sequence += 1
                step_id = await self._begin_step(tenant_id, run_id, name, sequence)
                try:
                    cost: StepCost = await node(ctx)
                except BridgeUnavailable as exc:
                    # Not a failure: the work is fine, the user's laptop is not
                    # reachable. Parking the run says so and leaves it resumable
                    # instead of making them start over.
                    await self._end_step(tenant_id, step_id, "failed", error=str(exc))
                    await self._set_status(
                        tenant_id, run_id, "awaiting_bridge", blocked_reason=str(exc)
                    )
                    return
                except Exception as exc:
                    await self._end_step(tenant_id, step_id, "failed", error=str(exc))
                    await self._set_status(tenant_id, run_id, "failed", error=str(exc))
                    logger.exception("run %s failed in node %s", run_id, name)
                    return
                await self._end_step(
                    tenant_id, step_id, "cached" if cost.cached else "succeeded",
                    output=cost.summary, bridge_calls=cost.bridge_calls,
                )
                await self._bump_progress(tenant_id, run_id, ctx, cost.bridge_calls)

            await self._persist_results(tenant_id, run_id, ctx)
            await self._set_status(tenant_id, run_id, "succeeded")
        except asyncio.CancelledError:
            await self._set_status(tenant_id, run_id, "cancelled")
            raise

    # ------------------------------------------------------------- plumbing --

    def _make_dispatch(self, tenant_id: UUID, run_id: UUID):
        async def dispatch(tool: str, args: dict[str, Any], *, deadline_s: float = 120.0) -> Any:
            conn = self._registry.require(tenant_id)
            outcome = await conn.dispatch(
                tool=tool, args=args, run_id=run_id,
                idempotency_key=self._registry.fresh_key(), deadline_s=deadline_s,
            )
            if not outcome.ok:
                raise RuntimeError(f"{tool}: {outcome.error}")
            return outcome.payload or {}
        return dispatch

    def _make_cache_lookup(self, tenant_id: UUID):
        async def lookup(usernames: list[str]) -> dict[str, Person]:
            if not usernames:
                return {}
            async with self._db.scoped(tenant_id=tenant_id) as conn:
                rows = await conn.fetch(
                    "SELECT linkedin_username, doc FROM people "
                    "WHERE tenant_id=$1 AND linkedin_username = ANY($2::text[]) "
                    # Anything older than this is refetched: a headline that is a
                    # year stale is worse than no headline, because it is acted on.
                    "AND refreshed_at > now() - interval '30 days'",
                    tenant_id, usernames,
                )
            return {r["linkedin_username"]: Person.model_validate_json(r["doc"]) for r in rows}
        return lookup

    def _make_cache_store(self, tenant_id: UUID, run_id: UUID):
        async def store(person: Person, from_cache: bool) -> None:
            async with self._db.scoped(tenant_id=tenant_id) as conn:
                await conn.execute(
                    "INSERT INTO people (tenant_id, dedupe_key, linkedin_urn, linkedin_username, "
                    " full_name, headline, location, doc, refreshed_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now()) "
                    "ON CONFLICT (tenant_id, dedupe_key) DO UPDATE SET "
                    " doc=EXCLUDED.doc, headline=EXCLUDED.headline, refreshed_at=now()",
                    tenant_id, person.identity.dedupe_key, person.identity.linkedin_urn,
                    person.identity.linkedin_username, person.identity.full_name,
                    person.identity.headline, person.identity.location,
                    person.model_dump_json(),
                )
        return store

    async def _persist_results(self, tenant_id: UUID, run_id: UUID, ctx: RunContext) -> None:
        async with self._db.scoped(tenant_id=tenant_id) as conn:
            for person in ctx.people:
                person_id = await conn.fetchval(
                    "SELECT id FROM people WHERE tenant_id=$1 AND dedupe_key=$2",
                    tenant_id, person.identity.dedupe_key,
                )
                if person_id is None:
                    continue
                await conn.execute(
                    "INSERT INTO run_people (run_id, person_id, tenant_id, score, "
                    " score_rationale, from_cache) VALUES ($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT (run_id, person_id) DO NOTHING",
                    run_id, person_id, tenant_id, person.score, person.score_rationale,
                    person.identity.linkedin_username in ctx.from_cache,
                )

    async def _begin_step(self, tenant_id: UUID, run_id: UUID, node: str, seq: int) -> UUID:
        step_id = uuid4()
        async with self._db.scoped(tenant_id=tenant_id) as conn:
            await conn.execute(
                "INSERT INTO run_steps (id, run_id, tenant_id, node, sequence, status) "
                "VALUES ($1,$2,$3,$4,$5,'running')",
                step_id, run_id, tenant_id, node, seq,
            )
        return step_id

    async def _end_step(
        self, tenant_id: UUID, step_id: UUID, status: str, *,
        output: dict | None = None, bridge_calls: int = 0, error: str | None = None,
    ) -> None:
        async with self._db.scoped(tenant_id=tenant_id) as conn:
            await conn.execute(
                "UPDATE run_steps SET status=$2, ended_at=now(), output_summary=$3, "
                " bridge_calls=$4, error=$5 WHERE id=$1",
                step_id, status, json.dumps(output or {}), bridge_calls, error,
            )

    async def _bump_progress(
        self, tenant_id: UUID, run_id: UUID, ctx: RunContext, calls: int
    ) -> None:
        async with self._db.scoped(tenant_id=tenant_id) as conn:
            await conn.execute(
                "UPDATE runs SET progress=$2, total_bridge_calls=total_bridge_calls+$3 "
                "WHERE id=$1",
                run_id,
                json.dumps({"found": len(ctx.usernames) + len(ctx.people),
                            "people": len(ctx.people),
                            "from_cache": len(ctx.from_cache)}),
                calls,
            )

    async def _set_status(
        self, tenant_id: UUID, run_id: UUID, status: str, *,
        started: bool = False, error: str | None = None, blocked_reason: str | None = None,
    ) -> None:
        async with self._db.scoped(tenant_id=tenant_id) as conn:
            await conn.execute(
                "UPDATE runs SET status=$2, error=$3, blocked_reason=$4, "
                " started_at = CASE WHEN $5 THEN now() ELSE started_at END, "
                " ended_at = CASE WHEN $2 IN ('succeeded','failed','cancelled') "
                "            THEN now() ELSE ended_at END "
                "WHERE id=$1",
                run_id, status, error, blocked_reason, started,
            )
