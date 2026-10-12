"""The six tools an agent sees. Coarse on purpose.

The client's model should be describing an outcome, not driving a scraper. If we
exposed the upstream server's nineteen fine-grained tools, every user's Claude
Code would have to orchestrate the sourcing loop itself — paying to pull raw
profile text into its own context, with no cache, no dedupe, no budget, and no
trace. That is the thing this product exists to not do.

So: say what you want, get a run id, poll it, read structured rows.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from pydantic import Field

from gtmos_graphs.brief import BriefInterpretation, parse_brief
from gtmos_schemas import ICP
from gtmos_control.workflows import LIBRARY

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP, *, db, registry, executor, identity) -> None:
    """`identity` resolves the calling principal to (tenant_id, user_id)."""

    @mcp.tool(
        title="Check LinkedIn connection",
        annotations={"readOnlyHint": True},
    )
    async def connect_linkedin() -> dict[str, Any]:
        """Check whether a LinkedIn bridge is connected, and how to connect one.

        Call this first if any workflow reports that it is waiting for a bridge.
        LinkedIn work runs on the user's own machine, so the platform needs a
        bridge paired and running before it can source anyone.
        """
        tenant_id, _ = await identity()
        conn = registry.get(tenant_id)
        if conn is None:
            return {
                "connected": False,
                "why": "No bridge is attached for this workspace.",
                "how_to_fix": [
                    "On the machine where you are logged into LinkedIn, run:",
                    "  uvx gtm-bridge pair --url <control-plane-url>",
                    "Approve the code in your browser, then run: uvx gtm-bridge run",
                ],
                "note": "Everything that is not LinkedIn works without a bridge.",
            }
        status = conn.status
        return {
            "connected": True,
            "linkedin_account": conn.hello.linkedin_username if conn.hello else None,
            "ready": conn.is_ready,
            "busy": bool(status and status.busy),
            "actions_today": status.actions_today if status else 0,
            "capabilities": len(conn.hello.capabilities) if conn.hello else 0,
        }

    @mcp.tool(title="List workflows", annotations={"readOnlyHint": True})
    async def list_workflows() -> list[dict[str, Any]]:
        """List the prebuilt GTM workflows available to run."""
        return [w.model_dump() for w in LIBRARY]

    @mcp.tool(title="Define a target")
    async def define_target(
        brief: Annotated[str, Field(description="Plain-English description of who to find")],
        name: Annotated[str | None, Field(description="Short label for this target")] = None,
        titles: Annotated[list[str] | None, Field(description="Job titles to match")] = None,
        locations: Annotated[list[str] | None, Field(description="Locations to filter to")] = None,
        industries: Annotated[list[str] | None, Field(description="Industries")] = None,
        keywords: Annotated[list[str] | None, Field(description="Extra keywords")] = None,
        headcount_min: int | None = None,
        headcount_max: int | None = None,
    ) -> dict[str, Any]:
        """Turn a plain-English brief into a reusable, structured target (an ICP).

        Pass the user's brief verbatim AND your structured reading of it. The
        brief is stored unchanged so the interpretation can be checked and
        corrected later rather than silently shaping every future run.
        """
        tenant_id, user_id = await identity()

        # The server parses the brief itself and merges the caller's reading over
        # it. Before this, the ICP was built *only* from these keyword arguments,
        # so a client that passed `brief=` alone produced an empty spec that
        # serialised to `interpreted_as: {}` — and `start_run` executed it,
        # spending six LinkedIn actions on a stale cached keyword. See the
        # Ourshopee regression in tests/test_brief_parser.py.
        interpretation = parse_brief(brief, hints={
            "titles": titles, "locations": locations, "industries": industries,
            "keywords": keywords, "headcount_min": headcount_min,
            "headcount_max": headcount_max,
        })
        icp = interpretation.icp

        if (refusal := interpretation.refusal()) is not None:
            # Deliberately *not* persisted. A target that cannot be executed is
            # not a draft to fix later, it is a question to answer now, and
            # storing it invites a later run to pick it up.
            refusal["brief_was"] = brief
            logger.info(
                "define_target refused: tenant=%s reason=%s confidence=%.2f",
                tenant_id, refusal["error"], interpretation.confidence,
            )
            return refusal

        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            target_id = await conn.fetchval(
                "INSERT INTO targets (tenant_id, kind, name, brief, spec, created_by) "
                "VALUES ($1,'icp',$2,$3,$4,$5) RETURNING id",
                tenant_id, name or brief[:60], brief, icp.model_dump_json(), user_id,
            )
        return {
            "target_id": str(target_id),
            "interpreted_as": icp.model_dump(exclude_defaults=True),
            "confidence": interpretation.confidence,
            "parser_version": interpretation.parser_version,
            "read_from": interpretation.provenance,
            "notes": interpretation.notes,
            "ambiguities": interpretation.ambiguities,
            "next": "Pass this target_id to start_run.",
        }

    @mcp.tool(title="Start a run")
    async def start_run(
        workflow: Annotated[str, Field(description="Workflow slug from list_workflows")],
        target_id: Annotated[str, Field(description="Target id from define_target")],
        max_people: Annotated[int, Field(ge=1, le=100, description="Cap on people to source")] = 10,
    ) -> dict[str, Any]:
        """Start a workflow. Returns immediately with a run_id — poll get_run.

        Runs are asynchronous because they must be: LinkedIn permits one action
        at a time per user, so sourcing 25 people is 26 serialized browser
        actions. Nothing useful can block on that.
        """
        tenant_id, user_id = await identity()
        async with db.scoped(tenant_id=tenant_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                "SELECT spec FROM targets WHERE id=$1 AND tenant_id=$2",
                UUID(target_id), tenant_id,
            )
        if row is None:
            return {"error": f"target {target_id} not found in this workspace"}

        icp = ICP.model_validate_json(row["spec"])

        # A second, independent gate. `define_target` already refuses an
        # unexecutable interpretation, so this looks redundant — it is not. Rows
        # written before the parser existed carry empty specs, `update_target`
        # can narrow one to nothing, and a target is a *reusable* object that
        # outlives the call that created it. The check belongs at the point where
        # the budget is actually spent, not only where the spec is born.
        if not BriefInterpretation(icp=icp, confidence=1.0).discriminating_fields:
            return {
                "error": "target_not_executable",
                "why": (
                    "This target carries no searchable criteria (no titles, companies, "
                    "industries or keywords). Running it would spend LinkedIn actions "
                    "on an unfiltered search."
                ),
                "target_id": target_id,
                "spec_was": icp.model_dump(exclude_defaults=True),
                "how_to_fix": "Call define_target again with a brief naming roles or companies.",
            }

        run_id = await executor.start(
            tenant_id=tenant_id, user_id=user_id, workflow=workflow, icp=icp,
            target_id=UUID(target_id), limits={"max_people": max_people},
        )
        return {
            "run_id": str(run_id),
            "status": "queued",
            "poll_with": "get_run",
            "estimated_bridge_calls": max_people + 1,
            "note": (
                f"LinkedIn actions run one at a time, so expect roughly "
                f"{max_people + 1} sequential steps."
            ),
        }

    @mcp.tool(title="Get run status", annotations={"readOnlyHint": True})
    async def get_run(run_id: str) -> dict[str, Any]:
        """Status and step-by-step trace of a run, including partial progress."""
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            run = await conn.fetchrow(
                "SELECT status, workflow, progress, blocked_reason, error, "
                " total_bridge_calls, created_at, started_at, ended_at "
                "FROM runs WHERE id=$1", UUID(run_id),
            )
            if run is None:
                return {"error": f"run {run_id} not found"}
            steps = await conn.fetch(
                "SELECT node, status, sequence, bridge_calls, output_summary, error, "
                " started_at, ended_at FROM run_steps WHERE run_id=$1 ORDER BY sequence",
                UUID(run_id),
            )
        return {
            "run_id": run_id,
            "status": run["status"],
            "workflow": run["workflow"],
            "progress": json.loads(run["progress"]) if isinstance(run["progress"], str) else run["progress"],
            "blocked_reason": run["blocked_reason"],
            "error": run["error"],
            "linkedin_actions_used": run["total_bridge_calls"],
            "trace": [
                {
                    "step": s["sequence"],
                    "node": s["node"],
                    "status": s["status"],
                    "linkedin_actions": s["bridge_calls"],
                    "output": json.loads(s["output_summary"]) if isinstance(s["output_summary"], str) else s["output_summary"],
                    "error": s["error"],
                    "ms": int((s["ended_at"] - s["started_at"]).total_seconds() * 1000)
                    if s["ended_at"] else None,
                }
                for s in steps
            ],
        }

    @mcp.tool(title="Get run results", annotations={"readOnlyHint": True})
    async def get_results(
        run_id: str,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Structured people found by a run, best match first, with evidence."""
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT p.full_name, p.headline, p.location, p.linkedin_username, "
                " rp.score, rp.score_rationale, rp.from_cache, p.doc "
                "FROM run_people rp JOIN people p ON p.id = rp.person_id "
                "WHERE rp.run_id=$1 ORDER BY rp.score DESC NULLS LAST LIMIT $2",
                UUID(run_id), limit,
            )
        people = []
        for r in rows:
            doc = json.loads(r["doc"]) if isinstance(r["doc"], str) else r["doc"]
            people.append({
                "name": r["full_name"],
                "headline": r["headline"],
                "location": r["location"],
                "linkedin": f"https://www.linkedin.com/in/{r['linkedin_username']}",
                "score": float(r["score"]) if r["score"] is not None else None,
                "why": r["score_rationale"],
                "served_from_cache": r["from_cache"],
                "emails": [
                    {"value": e["value"], "confidence": None, "sources":
                     [ev["provider"] for ev in e.get("evidence", [])]}
                    for e in (doc.get("emails") or [])
                ],
            })
        return {"run_id": run_id, "count": len(people), "people": people}
