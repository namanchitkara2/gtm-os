"""The memory tools an agent sees.

Same principle as ``tools.py``: coarse, outcome-shaped, and few. These nine
exist because of one finding — the expensive mistake in GTM work is not failing
to find someone, it is re-finding, re-researching and re-contacting someone the
team already has an answer about. Every tool here is a way of asking "what do we
already know?" before anything is spent.

The order they are meant to be used in:

    recall           before spending anything on a person or company
    rediscover       before sourcing anyone new at all
    enrich           when a field is genuinely missing, at the lowest price
    record_decision  the moment a verdict is reached, with its evidence
    remember         after a reply, so the objection survives the thread
    whats_changed    when looking for who is worth contacting today
    review_queue     what a human still has to look at
    decide_review    approve or reject one queued action
    spend_report     what the last 30 days cost, per qualified person
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from pydantic import Field

from gtmos_control import enrichment, memory

# Verdicts a caller may record. Kept in sync with the CHECK constraint on
# qualification_decisions rather than duplicated as free text.
_VERDICTS = ("qualified", "disqualified", "deferred", "needs_research")


def register_memory_tools(mcp: FastMCP, *, db, identity) -> None:
    """`identity` resolves the calling principal to (tenant_id, user_id)."""

    @mcp.tool(title="Recall everything known", annotations={"readOnlyHint": True})
    async def recall(
        entity_id: str,
        entity_type: Annotated[str, Field(pattern="^(person|company)$")] = "person",
    ) -> dict[str, Any]:
        """Everything this workspace knows about a person or company.

        Facts with their age and source, past qualification decisions and why,
        every interaction including what they objected to, live signals, cached
        research, and what has already been spent on them.

        Call this before enriching, researching or contacting anyone. It costs
        nothing, and it is the difference between "I see you moved to Stripe"
        and repeating an email the same person ignored four months ago.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            return await memory.recall(conn, entity_type, UUID(entity_id))

    @mcp.tool(title="Record a qualification decision")
    async def record_decision(
        entity_id: str,
        verdict: Annotated[str, Field(description=f"One of {_VERDICTS}")],
        reason: Annotated[str, Field(min_length=3, description="Why. In a sentence a colleague could act on.")],
        evidence_ids: Annotated[list[str] | None, Field(description="Evidence rows this rests on")] = None,
        entity_type: Annotated[str, Field(pattern="^(person|company)$")] = "person",
        target_id: str | None = None,
        confidence: Annotated[float | None, Field(ge=0, le=1)] = None,
    ) -> dict[str, Any]:
        """Write down why you qualified, passed on, or deferred someone.

        This is the most valuable row in the product. Every tool that later says
        "we already looked at this" is reading it, and it is what answers "why
        did we rule them out?" long after everyone involved has forgotten.

        A decision recorded by a model **must** cite evidence ids; the database
        rejects one that does not. A human may decide without them, because in
        that case the human is the evidence.
        """
        tenant_id, user_id = await identity()
        if verdict not in _VERDICTS:
            return {"error": f"verdict must be one of {_VERDICTS}"}
        async with db.scoped(tenant_id=tenant_id) as conn:
            try:
                decision_id = await memory.record_decision(
                    conn,
                    tenant_id=tenant_id,
                    entity_type=entity_type,
                    entity_id=UUID(entity_id),
                    verdict=verdict,
                    reason=reason,
                    actor_type="model",
                    actor_id="mcp-client",
                    evidence_ids=[UUID(e) for e in (evidence_ids or [])],
                    target_id=UUID(target_id) if target_id else None,
                    confidence=confidence,
                )
            except Exception as exc:
                if "model_verdicts_need_evidence" in str(exc):
                    return {
                        "error": "a model verdict needs evidence",
                        "why": "Recording a judgement with nothing behind it is how a "
                               "guess becomes a fact in someone else's quarter.",
                        "how_to_fix": "Pass evidence_ids, or have a person record this "
                                      "decision in the dashboard instead.",
                    }
                raise
        return {"decision_id": str(decision_id), "verdict": verdict, "recorded": True}

    @mcp.tool(title="Remember an interaction")
    async def remember(
        person_id: str,
        direction: Annotated[str, Field(pattern="^(outbound|inbound)$")],
        channel: Annotated[str, Field(pattern="^(linkedin_dm|linkedin_invite|email|call|meeting|other)$")],
        body: Annotated[str, Field(description="What was actually said")],
        subject: str | None = None,
        sentiment: Annotated[str | None, Field(description="positive|neutral|negative|out_of_office|unsubscribe")] = None,
        objection: Annotated[str | None, Field(description="price|timing|incumbent|not_hiring|…")] = None,
    ) -> dict[str, Any]:
        """Log a touch or a reply, with the objection if there was one.

        Objections are the part every tool throws away. "Not now, we just signed
        with a competitor for 18 months" is worth more than any enrichment field
        on the record, and it is worth exactly nothing if it stays in an inbox.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            interaction_id = await conn.fetchval(
                "INSERT INTO interactions "
                "(tenant_id, person_id, direction, channel, subject, body, sentiment, objection) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                tenant_id, UUID(person_id), direction, channel, subject, body, sentiment, objection,
            )
        return {"interaction_id": str(interaction_id), "recorded": True}

    @mcp.tool(title="What changed", annotations={"readOnlyHint": True})
    async def whats_changed(
        days: Annotated[int, Field(ge=1, le=365)] = 30,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """People and companies in the workspace that changed recently.

        Job changes, title moves, headcount jumps — ranked by how much the change
        is worth acting on. These come from diffing profiles we had already read
        for other reasons, so they are first-party observations with a before and
        an after, not a bought intent score nobody can validate.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            changes = await memory.whats_changed(conn, days=days, limit=limit)
        return {"window_days": days, "count": len(changes), "changes": changes}

    @mcp.tool(title="Rediscover people we already know", annotations={"readOnlyHint": True})
    async def rediscover(
        target_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """People already in memory who are worth another look, and why now.

        Costs nothing: no enrichment, no LinkedIn actions. Runs against people
        this workspace has already decided about, surfacing the ones whose
        situation has changed since that decision, plus deferrals that have aged.

        Worth running before sourcing anyone new. Roughly three quarters of the
        records sitting in a typical ATS are viable people nobody ever went back
        to, and the reason is always the same: the reason for passing was never
        written down anywhere a query could reach it.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            target = UUID(target_id) if target_id else None
            if target is not None:
                terms = await memory.target_terms(conn, target)
                if not terms:
                    # An unknown or term-less target would silently filter
                    # everything out, and an empty list looks identical to
                    # "nobody qualifies". Say which it is.
                    exists = await conn.fetchval(
                        "SELECT 1 FROM targets WHERE id=$1", target
                    )
                    return {
                        "error": "no usable target terms",
                        "why": "That target does not exist in this workspace."
                        if not exists
                        else "That target's spec has no titles, keywords or skills to match on.",
                        "how_to_fix": "Call rediscover without a target_id to search all memory.",
                    }
            people = await memory.rediscover(conn, target_id=target, limit=limit)
        return {
            "count": len(people),
            "cost_usd": 0.0,
            "linkedin_actions_used": 0,
            "people": people,
        }

    @mcp.tool(title="Review queue", annotations={"readOnlyHint": True})
    async def review_queue(
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Actions waiting on a human before they can run.

        By default, anything that reaches a person — a message, a connection
        request — waits here; enrichment and research do not. A workspace can
        change that per action class, but the default is deliberate: an
        automated system that can contact strangers unsupervised is one bad
        prompt away from being the user's problem, not ours.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            items = await memory.pending_approvals(conn, limit)
        return {
            "count": len(items),
            "pending": items,
            "note": "Approve or reject in the dashboard, or with decide_review.",
        }

    @mcp.tool(title="Decide a pending review")
    async def decide_review(
        approval_id: str,
        approved: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Approve or reject one queued action.

        Only a pending item can be decided, and the verdict is recorded against
        the deciding user. A decision cannot be rewritten later — which is the
        point of recording it.
        """
        tenant_id, user_id = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            changed = await memory.decide_approval(
                conn, approval_id=UUID(approval_id), approved=approved,
                user_id=user_id, reason=reason,
            )
        if not changed:
            return {"error": "not pending", "why": "Already decided, or expired."}
        return {"approval_id": approval_id, "status": "approved" if approved else "rejected"}

    @mcp.tool(title="Enrich a field")
    async def enrich(
        entity_id: str,
        field: Annotated[str, Field(description="email | phone | title | company | headcount | tech_stack")],
        entity_type: Annotated[str, Field(pattern="^(person|company)$")] = "person",
        budget_usd: Annotated[float | None, Field(ge=0, description="Cap for this one lookup")] = None,
    ) -> dict[str, Any]:
        """Find one field as cheaply as the confidence bar allows.

        Checks what we already know first, and if it is fresh enough for this
        field's policy, returns it without spending anything. Otherwise it works
        up from the cheapest source that could clear the bar and stops the
        moment one does.

        The return value includes what it cost, what the cache saved, and the
        trail of every source tried — so an expensive record can be explained
        rather than just billed.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            subject = await conn.fetchrow(
                "SELECT full_name, headline, linkedin_username FROM people WHERE id=$1",
                UUID(entity_id),
            ) if entity_type == "person" else None
            return await enrichment.enrich_field(
                conn,
                tenant_id=tenant_id,
                entity_type=entity_type,
                entity_id=UUID(entity_id),
                field=field,
                subject=dict(subject) if subject else {},
                budget_usd=budget_usd,
            )

    @mcp.tool(title="Spend report", annotations={"readOnlyHint": True})
    async def spend_report(
        days: Annotated[int, Field(ge=1, le=365)] = 30,
    ) -> dict[str, Any]:
        """What enrichment cost, what the cache saved, and cost per qualified person.

        Cost per qualified person is the number worth watching. Sends, opens and
        credits consumed are all easy to move in the wrong direction; this one is
        not.
        """
        tenant_id, _ = await identity()
        async with db.scoped(tenant_id=tenant_id) as conn:
            return await memory.spend_report(conn, days)
