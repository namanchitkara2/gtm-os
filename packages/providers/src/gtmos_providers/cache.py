"""The cheapest provider, and the reason the product has a margin.

Freshness is a product decision, not a constant. B2B contact data decays at
roughly 2.1% a month — about 22.5% a year, and ZoomInfo's own analysis puts it
at 25-30% — but the average hides the thing that matters: **65.8% of job titles
change annually**, while a direct phone number decays at 15-20%
(docs/product-research.md). Treating those two fields with one TTL either
re-buys phone numbers that were fine or serves titles that are a year out of
date, and the second failure is the one that puts a wrong fact in an outbound
message.

So TTL is per field, per tenant, and lives in ``field_policies``. This module
holds the decision logic; the app supplies the rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CachedValue(BaseModel):
    """A value we already hold, with the two facts that decide its fate."""

    model_config = ConfigDict(frozen=True)

    value: Any
    confidence: float = Field(ge=0.0, le=1.0)
    observed_at: datetime
    source: str

    def age_days(self, *, now: datetime | None = None) -> float:
        return ((now or _utcnow()) - self.observed_at).total_seconds() / 86400.0


class FieldPolicy(BaseModel):
    """How long a field stays true, and how sure we need to be about it."""

    model_config = ConfigDict(frozen=True)

    field: str
    ttl_days: int = Field(gt=0)
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    max_cost_usd: float | None = None


class CacheReader(Protocol):
    """Whatever can answer 'what do we already know about this field?'

    A protocol rather than a repository import so this package stays free of
    database concerns and can be tested with a dict.
    """

    async def current(
        self, *, entity_type: str, entity_id: str, field: str
    ) -> CachedValue | None: ...


class Verdict(BaseModel):
    """What the cache says the router should do next."""

    model_config = ConfigDict(frozen=True)

    use_cache: bool
    cached: CachedValue | None = None
    reason: str

    @property
    def age_days(self) -> float | None:
        return None if self.cached is None else self.cached.age_days()


def assess(
    cached: CachedValue | None,
    policy: FieldPolicy,
    *,
    now: datetime | None = None,
) -> Verdict:
    """Decide whether what we hold is good enough to use without spending.

    Three ways to fail, and they are reported separately because they lead to
    different product behaviour: nothing held (buy it), too old (re-buy it), or
    held but under-confident (escalate to a better source, not the same one).

    Note what this deliberately does *not* do: it never silently serves a stale
    value. A caller that wants the stale value anyway can read ``cached`` off the
    verdict — but it has to do that on purpose, with the age in its hand.
    """
    if cached is None:
        return Verdict(use_cache=False, reason="not_cached")

    now = now or _utcnow()
    age = cached.age_days(now=now)
    if age > policy.ttl_days:
        return Verdict(
            use_cache=False,
            cached=cached,
            reason=f"stale:{age:.0f}d>{policy.ttl_days}d",
        )
    if cached.confidence < policy.min_confidence:
        return Verdict(
            use_cache=False,
            cached=cached,
            reason=f"low_confidence:{cached.confidence:.2f}<{policy.min_confidence:.2f}",
        )
    return Verdict(use_cache=True, cached=cached, reason=f"fresh:{age:.0f}d")


def stale_after(policy: FieldPolicy, observed_at: datetime) -> datetime:
    """When a value observed at this time stops being usable.

    Used by the refresh queue to rank: an entity whose title expires tomorrow is
    a better use of a scarce LinkedIn action than one that expires in a month.
    """
    return observed_at + timedelta(days=policy.ttl_days)
