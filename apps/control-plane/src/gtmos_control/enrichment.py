"""Where the router meets the database.

``packages/providers`` decides *which* source to ask and when to stop. This
module is the only place allowed to call it, and it does the three things the
package deliberately does not: read the tenant's freshness policy, read what we
already know, and write down what happened.

ADR-008 says every enrichment call site goes through the router. That is only
true if there is exactly one call site, so this is it. A direct provider call
anywhere else in the control plane is a bug, not a shortcut — it would spend a
customer's credits without a ledger row, which is the failure the whole package
exists to prevent.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg
from gtmos_providers import CachedValue, FieldPolicy, Resolution, Router, default_waterfall

from gtmos_control import memory

logger = logging.getLogger(__name__)

#: Used when a tenant has no row in ``field_policies`` for this field. Matches
#: the seeds in 0002 for the fields we know about, and is conservative for any
#: field nobody has set a policy for yet.
_FALLBACK = FieldPolicy(field="", ttl_days=90, min_confidence=0.7)


class ProvenanceCache:
    """The cache half of the waterfall, backed by ``field_provenance``.

    Reads the current (non-superseded) observation. Freshness is not decided
    here — ``gtmos_providers.cache.assess`` does that against the tenant's
    policy, so the rule lives in one place and this class stays a lookup.
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def current(
        self, *, entity_type: str, entity_id: str, field: str
    ) -> CachedValue | None:
        row = await self._conn.fetchrow(
            "SELECT value, source, confidence, observed_at FROM field_provenance "
            "WHERE entity_type=$1 AND entity_id=$2 AND field=$3 AND superseded_by IS NULL "
            "ORDER BY observed_at DESC LIMIT 1",
            entity_type, UUID(entity_id), field,
        )
        if row is None:
            return None
        value = row["value"]
        return CachedValue(
            value=json.loads(value) if isinstance(value, str) else value,
            confidence=float(row["confidence"]),
            observed_at=row["observed_at"],
            source=row["source"],
        )


async def load_policy(conn: asyncpg.Connection, field: str) -> FieldPolicy:
    row = await conn.fetchrow(
        "SELECT ttl_days, min_confidence, max_cost_usd FROM field_policies WHERE field=$1",
        field,
    )
    if row is None:
        return _FALLBACK.model_copy(update={"field": field})
    return FieldPolicy(
        field=field,
        ttl_days=row["ttl_days"],
        min_confidence=float(row["min_confidence"]),
        max_cost_usd=float(row["max_cost_usd"]) if row["max_cost_usd"] is not None else None,
    )


async def _write_ledger(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    entity_type: str,
    entity_id: UUID,
    resolution: Resolution,
    run_id: UUID | None,
) -> None:
    """Record every attempt, including the ones that spent nothing.

    The skipped rows are the point. A ledger that only lists charges can tell a
    customer what they spent but never what the cache saved them, and the second
    number is the one that justifies the product.
    """
    await conn.executemany(
        "INSERT INTO enrichment_ledger (tenant_id, entity_type, entity_id, field, provider, "
        " outcome, credits, cost_usd, confidence, latency_ms, run_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
        [
            (
                tenant_id, entity_type, entity_id, a.field, a.provider, a.outcome,
                a.credits, a.cost_usd, a.confidence, a.latency_ms, run_id,
            )
            for a in resolution.attempts
        ],
    )


async def enrich_field(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    entity_type: str,
    entity_id: UUID,
    field: str,
    subject: dict[str, Any],
    clients: dict[str, Any] | None = None,
    budget_usd: float | None = None,
    run_id: UUID | None = None,
) -> dict[str, Any]:
    """Resolve one field as cheaply as the confidence bar allows, and record it.

    ``clients`` carries the tenant's BYOK provider clients. With none of them
    configured the waterfall still runs and still writes a ledger — it simply
    finds nothing, which is the honest outcome and is visibly different from
    "we never looked".
    """
    clients = clients or {}
    policy = await load_policy(conn, field)
    router = Router(
        default_waterfall(apollo=clients.get("apollo"), premium=clients.get("premium")),
        cache=ProvenanceCache(conn),
    )
    resolution = await router.resolve(
        field=field,
        entity_type=entity_type,
        entity_id=str(entity_id),
        subject=subject,
        policy=policy,
        budget_usd=budget_usd,
    )

    await _write_ledger(
        conn, tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id,
        resolution=resolution, run_id=run_id,
    )

    # A cache hit is already recorded as a fact; re-recording it would reset its
    # age and destroy the only thing that makes the cache trustworthy.
    if resolution.found and not resolution.from_cache:
        await memory.record_fact(
            conn,
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            value=resolution.value,
            source=resolution.source or "unknown",
            confidence=resolution.confidence,
            run_id=run_id,
        )

    return {
        "field": field,
        "found": resolution.found,
        "value": resolution.value,
        "confidence": round(resolution.confidence, 3),
        "source": resolution.source,
        "served_from_cache": resolution.from_cache,
        "usd_spent": resolution.spend_usd,
        "usd_saved": resolution.saved_usd,
        "trail": [
            {"provider": a.provider, "outcome": a.outcome, "usd": a.cost_usd, "note": a.note}
            for a in resolution.attempts
        ],
    }
