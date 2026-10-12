"""Reads and writes over the memory layer.

The tools in ``memory_tools`` are the product surface; this module is the SQL
behind them. It is separate for one reason: the queries here decide what the
model is allowed to believe, and they should be readable on their own without a
tool decorator in the way.

Two conventions run through everything below.

**Age travels with every fact.** No function returns a value without how old it
is. Contact data decays 22-30% a year and 65.8% of job titles change annually
(docs/product-research.md), so a title with no date attached is not information,
it is a guess with a confident font.

**Nothing here overwrites.** A newer observation supersedes an older one and a
changed mind is a second decision row. The history is what change detection
reads, and it is what answers "why did we qualify this account?" six months
later when the person who decided has left.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg


def _age_days(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return round((datetime.now(timezone.utc) - ts).total_seconds() / 86400.0, 1)


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# --------------------------------------------------------------- provenance --


async def current_facts(conn: asyncpg.Connection, entity_type: str, entity_id: UUID) -> list[dict]:
    """Every field we currently believe, with its source, age and confidence.

    ``superseded_by IS NULL`` is what makes this the current view of an
    append-only table — the older rows stay for the differ.
    """
    rows = await conn.fetch(
        "SELECT DISTINCT ON (field) field, value, source, confidence, observed_at "
        "FROM field_provenance "
        "WHERE entity_type=$1 AND entity_id=$2 AND superseded_by IS NULL "
        "ORDER BY field, observed_at DESC",
        entity_type, entity_id,
    )
    return [
        {
            "field": r["field"],
            "value": _loads(r["value"]),
            "source": r["source"],
            "confidence": float(r["confidence"]),
            "observed_at": r["observed_at"].isoformat(),
            "age_days": _age_days(r["observed_at"]),
        }
        for r in rows
    ]


async def record_fact(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    entity_type: str,
    entity_id: UUID,
    field: str,
    value: Any,
    source: str,
    confidence: float,
    run_id: UUID | None = None,
) -> UUID:
    """Observe a field. Supersedes the previous observation rather than updating it."""
    new_id = await conn.fetchval(
        "INSERT INTO field_provenance "
        "(tenant_id, entity_type, entity_id, field, value, source, confidence, run_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
        tenant_id, entity_type, entity_id, field, json.dumps(value), source, confidence, run_id,
    )
    await conn.execute(
        "UPDATE field_provenance SET superseded_by=$1 "
        "WHERE entity_type=$2 AND entity_id=$3 AND field=$4 AND id<>$1 AND superseded_by IS NULL",
        new_id, entity_type, entity_id, field,
    )
    return new_id


# ------------------------------------------------------------------ recall --


async def recall(conn: asyncpg.Connection, entity_type: str, entity_id: UUID) -> dict[str, Any]:
    """Everything the workspace knows about one entity, in one read.

    This is the tool an agent should call *before* it spends anything. The
    ordering is deliberate — facts, then what we decided, then what was actually
    said — because the most common mistake an agent makes is re-researching
    someone the team already disqualified for a reason nobody wrote down.
    """
    facts = await current_facts(conn, entity_type, entity_id)

    decisions = await conn.fetch(
        "SELECT verdict, reason, actor_type, actor_id, confidence, decided_at, evidence_ids "
        "FROM qualification_decisions WHERE entity_type=$1 AND entity_id=$2 "
        "ORDER BY decided_at DESC LIMIT 20",
        entity_type, entity_id,
    )

    interactions: list[asyncpg.Record] = []
    if entity_type == "person":
        interactions = await conn.fetch(
            "SELECT direction, channel, occurred_at, subject, sentiment, objection, "
            " left(coalesce(body,''), 400) AS excerpt "
            "FROM interactions WHERE person_id=$1 ORDER BY occurred_at DESC LIMIT 20",
            entity_id,
        )

    signals = await conn.fetch(
        "SELECT signal_type, summary, strength, detected_at, acted_on_at "
        "FROM signals WHERE entity_type=$1 AND entity_id=$2 "
        "AND (expires_at IS NULL OR expires_at > now()) "
        "ORDER BY detected_at DESC LIMIT 20",
        entity_type, entity_id,
    )

    research = await conn.fetch(
        "SELECT kind, body, produced_by, created_at, expires_at "
        "FROM research_artifacts WHERE entity_type=$1 AND entity_id=$2 "
        "ORDER BY created_at DESC LIMIT 10",
        entity_type, entity_id,
    )

    spend = await conn.fetchrow(
        "SELECT coalesce(sum(cost_usd) FILTER (WHERE outcome IN ('hit','miss')),0) AS spent, "
        " coalesce(sum(cost_usd) FILTER (WHERE outcome='skipped_cache'),0) AS saved, "
        " count(*) AS attempts "
        "FROM enrichment_ledger WHERE entity_type=$1 AND entity_id=$2",
        entity_type, entity_id,
    )

    return {
        "entity": {"type": entity_type, "id": str(entity_id)},
        "known_facts": facts,
        # Surfaced separately so a model does not have to scan ages itself to
        # notice that what it is about to assert is a year old.
        "stale_fields": [f["field"] for f in facts if (f["age_days"] or 0) > 180],
        "decisions": [
            {
                "verdict": d["verdict"],
                "reason": d["reason"],
                "by": f"{d['actor_type']}:{d['actor_id']}" if d["actor_id"] else d["actor_type"],
                "confidence": float(d["confidence"]) if d["confidence"] is not None else None,
                "decided_at": d["decided_at"].isoformat(),
                "days_ago": _age_days(d["decided_at"]),
                "evidence_count": len(d["evidence_ids"] or []),
            }
            for d in decisions
        ],
        "interactions": [
            {
                "direction": i["direction"],
                "channel": i["channel"],
                "when": i["occurred_at"].isoformat(),
                "days_ago": _age_days(i["occurred_at"]),
                "subject": i["subject"],
                "excerpt": i["excerpt"],
                "sentiment": i["sentiment"],
                "objection": i["objection"],
            }
            for i in interactions
        ],
        "signals": [
            {
                "type": s["signal_type"],
                "summary": s["summary"],
                "strength": float(s["strength"]),
                "detected_at": s["detected_at"].isoformat(),
                "acted_on": s["acted_on_at"] is not None,
            }
            for s in signals
        ],
        "research": [
            {
                "kind": r["kind"],
                "body": r["body"],
                "by": r["produced_by"],
                "days_ago": _age_days(r["created_at"]),
                "expired": r["expires_at"] is not None
                and r["expires_at"] < datetime.now(timezone.utc),
            }
            for r in research
        ],
        "spend": {
            "usd_spent": float(spend["spent"]),
            "usd_saved_by_cache": float(spend["saved"]),
            "enrichment_attempts": spend["attempts"],
        },
    }


# --------------------------------------------------------------- decisions --


async def record_decision(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    entity_type: str,
    entity_id: UUID,
    verdict: str,
    reason: str,
    actor_type: str,
    actor_id: str | None = None,
    evidence_ids: list[UUID] | None = None,
    target_id: UUID | None = None,
    confidence: float | None = None,
    run_id: UUID | None = None,
) -> UUID:
    """Write a qualification verdict.

    The database refuses a ``model`` verdict with no evidence ids
    (``model_verdicts_need_evidence``). That constraint is the enforcement point
    for ADR-007, and it is checked here rather than trusted to a prompt, so it
    holds for every caller including ones written later by someone else.
    """
    return await conn.fetchval(
        "INSERT INTO qualification_decisions "
        "(tenant_id, entity_type, entity_id, target_id, verdict, reason, evidence_ids, "
        " actor_type, actor_id, confidence, run_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id",
        tenant_id, entity_type, entity_id, target_id, verdict, reason,
        evidence_ids or [], actor_type, actor_id, confidence, run_id,
    )


# ------------------------------------------------------- change detection --


async def snapshot(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    entity_type: str,
    entity_id: UUID,
    doc: dict[str, Any],
    source: str,
    run_id: UUID | None = None,
) -> dict[str, Any]:
    """Store a snapshot and report what changed since the last one.

    ADR-009: this runs on reads we were already making for another reason, so
    change detection costs no extra LinkedIn action. The digest short-circuits
    the common case — most re-reads find nothing new, and comparing one string
    is cheaper than walking two JSON documents.
    """
    watched = {k: doc.get(k) for k in ("title", "company", "headline", "location", "headcount")}
    digest = json.dumps(watched, sort_keys=True, default=str)

    previous = await conn.fetchrow(
        "SELECT doc, digest, captured_at FROM entity_snapshots "
        "WHERE entity_type=$1 AND entity_id=$2 ORDER BY captured_at DESC LIMIT 1",
        entity_type, entity_id,
    )
    await conn.execute(
        "INSERT INTO entity_snapshots (tenant_id, entity_type, entity_id, doc, digest, source, run_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
        tenant_id, entity_type, entity_id, json.dumps(doc, default=str), digest, source, run_id,
    )
    if previous is None:
        return {"first_snapshot": True, "changes": []}
    if previous["digest"] == digest:
        return {"first_snapshot": False, "changes": []}

    before = _loads(previous["doc"]) or {}
    changes = [
        {"field": k, "from": before.get(k), "to": v, "since": previous["captured_at"].isoformat()}
        for k, v in watched.items()
        if before.get(k) != v
    ]

    # A diff nobody turns into a signal is a diff nobody sees. Raising them here
    # rather than in a caller is deliberate: this is the only place that knows a
    # change happened, and a change detected but not recorded is indistinguishable
    # from one never detected at all.
    signals: list[str] = []
    for change in changes:
        signal_type = FIELD_SIGNALS.get(change["field"])
        if signal_type is None:
            continue
        raised = await raise_signal(
            conn,
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            signal_type=signal_type,
            summary=f"{change['field']}: {change['from']!r} → {change['to']!r}",
            source="snapshot_diff",
        )
        if raised is not None:
            signals.append(signal_type)

    return {"first_snapshot": False, "changes": changes, "signals_raised": signals}


#: Which watched field produces which signal. ``headcount`` is deliberately
#: absent from a person-level diff: a company's headcount moving is a company
#: signal, and raising it once per employee we happen to re-read would flood the
#: feed with the same fact.
FIELD_SIGNALS = {
    "company": "company_change",
    "title": "title_change",
    "headline": "headline_change",
    "location": "location_change",
}

#: What a change is worth chasing. A person moving company is the warmest signal
#: in outbound and the most common reason a candidate becomes available; a
#: location change rarely means anything on its own.
SIGNAL_STRENGTH = {
    "company_change": 0.9,
    "title_change": 0.7,
    "headcount_jump": 0.6,
    "headline_change": 0.3,
    "location_change": 0.2,
}


async def raise_signal(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    entity_type: str,
    entity_id: UUID,
    signal_type: str,
    summary: str,
    source: str,
    evidence_ids: list[UUID] | None = None,
    expires_days: int = 90,
) -> UUID | None:
    """Record a typed change. Returns None if this exact signal is already known."""
    return await conn.fetchval(
        "INSERT INTO signals (tenant_id, entity_type, entity_id, signal_type, strength, "
        " summary, evidence_ids, source, expires_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now() + ($9 || ' days')::interval) "
        "ON CONFLICT DO NOTHING RETURNING id",
        tenant_id, entity_type, entity_id, signal_type,
        SIGNAL_STRENGTH.get(signal_type, 0.5), summary, evidence_ids or [], source, str(expires_days),
    )


async def whats_changed(conn: asyncpg.Connection, *, days: int, limit: int) -> list[dict]:
    """Live signals across the workspace, strongest first."""
    rows = await conn.fetch(
        "SELECT s.signal_type, s.summary, s.strength, s.detected_at, s.entity_type, s.entity_id, "
        " coalesce(p.full_name, c.name) AS label, p.linkedin_username "
        "FROM signals s "
        "LEFT JOIN people p ON s.entity_type='person' AND p.id = s.entity_id "
        "LEFT JOIN companies c ON s.entity_type='company' AND c.id = s.entity_id "
        "WHERE s.acted_on_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > now()) "
        " AND s.detected_at > now() - ($1 || ' days')::interval "
        "ORDER BY s.strength DESC, s.detected_at DESC LIMIT $2",
        str(days), limit,
    )
    return [
        {
            "who": r["label"],
            "entity": {"type": r["entity_type"], "id": str(r["entity_id"])},
            "linkedin": f"https://www.linkedin.com/in/{r['linkedin_username']}"
            if r["linkedin_username"] else None,
            "signal": r["signal_type"],
            "summary": r["summary"],
            "strength": float(r["strength"]),
            "days_ago": _age_days(r["detected_at"]),
        }
        for r in rows
    ]


# ------------------------------------------------------------ rediscovery --


async def target_terms(conn: asyncpg.Connection, target_id: UUID) -> list[str]:
    """The words worth matching a remembered person against, from a target's spec.

    Deliberately shallow. A full re-score of memory against a new ICP is the
    scoring engine's job and it costs model calls; this is a text filter that
    narrows "everyone we ever decided about" to "people whose headline plausibly
    relates to this role". Getting the shortlist wrong here is cheap — a human
    is reading it — and the alternative, running a scorer over the whole
    workspace, is not.
    """
    row = await conn.fetchrow("SELECT kind, spec FROM targets WHERE id=$1", target_id)
    if row is None:
        return []
    spec = _loads(row["spec"]) or {}
    if row["kind"] == "role":
        fields = ("role_title", "seniority")
        lists = ("must_have_skills", "nice_to_have_skills")
    else:
        fields = ()
        lists = ("titles", "keywords", "industries", "tech_stack")
    terms = [spec.get(f) for f in fields if spec.get(f)]
    for key in lists:
        terms.extend(spec.get(key) or [])
    # Single words like "growth" match too much to be useful as a filter and
    # two-character fragments match everything.
    return [t.strip() for t in terms if isinstance(t, str) and len(t.strip()) > 2]


async def rediscover(
    conn: asyncpg.Connection, *, target_id: UUID | None, limit: int
) -> list[dict]:
    """People we already hold who are worth another look, optionally for a target.

    The cheapest pipeline in the product: no enrichment spend, no LinkedIn
    actions, only memory. 75% of ATS records contain viable candidates nobody
    ever re-engaged (docs/product-research.md), and the reason is that the
    conversation and the reason for passing were never stored — which is exactly
    what this table now holds.

    Ranked by *why it is different now* rather than by fit score: a strong signal
    since the decision, then a deferral that has aged out, then simple age. A
    candidate we disqualified for a good reason should stay disqualified unless
    something about them changed.

    With a ``target_id``, the list is narrowed to people whose headline matches
    that target's terms, plus anyone already judged against it. Without one, it
    is the whole workspace.
    """
    terms = await target_terms(conn, target_id) if target_id else []
    rows = await conn.fetch(
        """
        WITH last_decision AS (
            SELECT DISTINCT ON (entity_id) entity_id, verdict, reason, decided_at, target_id
            FROM qualification_decisions
            WHERE entity_type='person'
            ORDER BY entity_id, decided_at DESC
        )
        SELECT p.id, p.full_name, p.headline, p.linkedin_username,
               d.verdict, d.reason, d.decided_at,
               s.signal_type, s.summary AS signal_summary, s.strength,
               (SELECT max(occurred_at) FROM interactions i WHERE i.person_id = p.id) AS last_touch
        FROM people p
        JOIN last_decision d ON d.entity_id = p.id
        LEFT JOIN LATERAL (
            SELECT signal_type, summary, strength FROM signals sg
            WHERE sg.entity_type='person' AND sg.entity_id = p.id
              -- >= rather than >, because two writes inside one transaction share
              -- a now() and are therefore indistinguishable in time. The two
              -- errors are not symmetric: a false positive resurfaces someone
              -- once, a false negative means a job change never resurfaces them
              -- at all, which is the exact failure this feature exists to fix.
              AND sg.detected_at >= d.decided_at
              AND (sg.expires_at IS NULL OR sg.expires_at > now())
            ORDER BY sg.strength DESC LIMIT 1
        ) s ON TRUE
        WHERE (d.verdict IN ('qualified','deferred')
               OR (d.verdict='disqualified' AND s.signal_type IS NOT NULL))
          -- No target: the whole workspace. With one: people already judged
          -- against it, or whose headline mentions any of its terms.
          AND ($2::uuid IS NULL
               OR d.target_id = $2::uuid
               OR ($3::text[] IS NOT NULL
                   AND p.headline ILIKE ANY (SELECT '%' || t || '%' FROM unnest($3::text[]) AS t)))
        ORDER BY coalesce(s.strength, 0) DESC, d.decided_at ASC
        LIMIT $1
        """,
        limit, target_id, terms or None,
    )
    out = []
    for r in rows:
        why = []
        if r["signal_type"]:
            why.append(f"{r['signal_type']}: {r['signal_summary']}")
        if r["verdict"] == "deferred":
            why.append(f"was deferred {_age_days(r['decided_at'])} days ago: {r['reason']}")
        elif r["verdict"] == "qualified":
            why.append(f"qualified but never converted: {r['reason']}")
        elif r["verdict"] == "disqualified":
            why.append(f"was ruled out ({r['reason']}) — but something changed since")
        out.append({
            "person_id": str(r["id"]),
            "name": r["full_name"],
            "headline": r["headline"],
            "linkedin": f"https://www.linkedin.com/in/{r['linkedin_username']}"
            if r["linkedin_username"] else None,
            "last_verdict": r["verdict"],
            "why_now": " · ".join(why),
            "days_since_last_touch": _age_days(r["last_touch"]),
            "cost_to_surface_usd": 0.0,
        })
    return out


# ------------------------------------------------------------- approvals --


async def approval_mode(conn: asyncpg.Connection, action_class: str) -> str:
    """What the tenant's policy says about this class of action.

    Defaults to requiring approval when no policy row exists. An unknown action
    class is one nobody has thought about yet, and the safe reading of "nobody
    has thought about this" is not "send it".
    """
    mode = await conn.fetchval(
        "SELECT mode FROM approval_policies WHERE action_class=$1", action_class
    )
    return mode or "require_approval"


async def request_approval(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    action_class: str,
    subject_type: str,
    subject_id: UUID,
    payload: dict[str, Any],
    expires_days: int = 7,
) -> UUID:
    return await conn.fetchval(
        "INSERT INTO approvals (tenant_id, action_class, subject_type, subject_id, payload, expires_at) "
        "VALUES ($1,$2,$3,$4,$5, now() + ($6 || ' days')::interval) RETURNING id",
        tenant_id, action_class, subject_type, subject_id, json.dumps(payload, default=str),
        str(expires_days),
    )


async def pending_approvals(conn: asyncpg.Connection, limit: int) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, action_class, subject_type, subject_id, payload, requested_at, expires_at "
        "FROM approvals WHERE status='pending' "
        " AND (expires_at IS NULL OR expires_at > now()) "
        "ORDER BY requested_at LIMIT $1",
        limit,
    )
    return [
        {
            "approval_id": str(r["id"]),
            "action_class": r["action_class"],
            "subject": {"type": r["subject_type"], "id": str(r["subject_id"])},
            "what": _loads(r["payload"]),
            "waiting_days": _age_days(r["requested_at"]),
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        }
        for r in rows
    ]


async def decide_approval(
    conn: asyncpg.Connection,
    *,
    approval_id: UUID,
    approved: bool,
    user_id: UUID | None,
    reason: str | None,
) -> bool:
    """Record a human verdict. Only a pending approval can be decided.

    The ``status='pending'`` guard is what stops a second call from silently
    rewriting a decision that has already been acted on — the EU AI Act wants a
    reviewer with genuine authority, and an overwritable record is not one.
    """
    result = await conn.execute(
        "UPDATE approvals SET status=$1, decided_by=$2, decided_at=now(), reason=$3 "
        "WHERE id=$4 AND status='pending'",
        "approved" if approved else "rejected", user_id, reason, approval_id,
    )
    return result.endswith("1")


# ---------------------------------------------------------------- spend --


async def spend_report(conn: asyncpg.Connection, days: int) -> dict[str, Any]:
    """Cost per outcome, which is the only enrichment metric worth reporting."""
    totals = await conn.fetchrow(
        "SELECT coalesce(sum(cost_usd) FILTER (WHERE outcome IN ('hit','miss')),0) AS spent, "
        " coalesce(sum(cost_usd) FILTER (WHERE outcome='skipped_cache'),0) AS saved, "
        " count(*) FILTER (WHERE outcome='hit') AS hits, "
        " count(*) FILTER (WHERE outcome='miss') AS misses, "
        " count(*) FILTER (WHERE outcome='skipped_cache') AS cache_hits, "
        " count(*) FILTER (WHERE outcome='skipped_budget') AS budget_stops "
        "FROM enrichment_ledger WHERE created_at > now() - ($1 || ' days')::interval",
        str(days),
    )
    by_provider = await conn.fetch(
        "SELECT provider, field, count(*) AS calls, "
        " coalesce(sum(cost_usd),0) AS cost, "
        " round(100.0 * count(*) FILTER (WHERE outcome='hit') / nullif(count(*),0), 1) AS hit_pct "
        "FROM enrichment_ledger WHERE created_at > now() - ($1 || ' days')::interval "
        "GROUP BY provider, field ORDER BY cost DESC LIMIT 25",
        str(days),
    )
    qualified = await conn.fetchval(
        "SELECT count(DISTINCT entity_id) FROM qualification_decisions "
        "WHERE verdict='qualified' AND decided_at > now() - ($1 || ' days')::interval",
        str(days),
    )
    spent = float(totals["spent"])
    return {
        "window_days": days,
        "usd_spent": round(spent, 4),
        "usd_saved_by_cache": round(float(totals["saved"]), 4),
        "cache_hit_rate_pct": round(
            100.0 * totals["cache_hits"] / max(totals["cache_hits"] + totals["hits"] + totals["misses"], 1), 1
        ),
        "qualified_people": qualified,
        # The number a buyer actually compares against a Clay invoice.
        "cost_per_qualified_usd": round(spent / qualified, 4) if qualified else None,
        "budget_stops": totals["budget_stops"],
        "by_provider": [
            {
                "provider": r["provider"],
                "field": r["field"],
                "calls": r["calls"],
                "usd": round(float(r["cost"]), 4),
                "hit_rate_pct": float(r["hit_pct"]) if r["hit_pct"] is not None else None,
            }
            for r in by_provider
        ],
    }
