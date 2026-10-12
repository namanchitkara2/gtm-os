"""The waterfall: cache first, then the cheapest source that clears the bar.

ADR-008. This is the foundation the rest of the product spends through, and the
order it enforces is the whole feature:

    cache within TTL  ->  cheapest provider that can clear min_confidence
                      ->  stop as soon as the bar is met
                      ->  escalate only while confidence is still short

Why this is worth a package rather than a helper function: the top complaint
about the category leader is credit burn — 42% of Clay's negative reviews cite
it, with users reporting $800 in a week while still learning the tool — and the
cause is that a waterfall is easy to build and hard to *stop*. Every step runs
because there is no shared notion of "we already have enough".

We can ship the opposite because of how we charge. Enrichment keys are BYOK, so
the customer pays the provider directly and every lookup this module declines to
make is money that stays with them and costs us nothing. No per-credit vendor can
copy that without cannibalising its own revenue line.

One deliberate non-goal: this module does not decide *whether* a field is worth
having. That is the caller's judgement. It decides what the cheapest honest way
to get it is, and it records what happened either way.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import Answer, Attempt, Provider
from .cache import CachedValue, CacheReader, FieldPolicy, assess


class Resolution(BaseModel):
    """The outcome of shopping for one field, and the receipt for it."""

    model_config = ConfigDict(frozen=True)

    field: str
    value: Any | None = None
    confidence: float = 0.0
    source: str | None = None
    from_cache: bool = False
    #: Every step taken, including the ones that spent nothing. This is what
    #: gets written to ``enrichment_ledger``.
    attempts: list[Attempt] = Field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.value is not None

    #: Outcomes where a vendor call was actually made and therefore billed. A
    #: miss counts: most providers charge for the lookup, not for the answer.
    #: ``skipped_*`` rows carry a cost too, but it is the cost we *avoided*, and
    #: summing the two together would report a saving as a charge.
    _BILLED = ("hit", "miss")

    @property
    def spend_usd(self) -> float:
        return round(
            sum(a.cost_usd for a in self.attempts if a.outcome in self._BILLED), 4
        )

    @property
    def saved_usd(self) -> float:
        """What not calling anyone was worth.

        Only meaningful on a cache hit, and it is a modelled number rather than
        a measured one: the cheapest provider we would otherwise have called.
        Reported so a customer can see the cache working, labelled so nobody
        mistakes it for a bank statement.
        """
        skipped = [a for a in self.attempts if a.outcome == "skipped_cache"]
        return round(sum(a.cost_usd for a in skipped), 4)


class Router:
    """Routes one field request across cache and providers.

    Stateless apart from its provider list, so a tenant's BYOK credentials
    decide which providers exist for that call rather than which code path runs.
    """

    def __init__(self, providers: list[Provider], cache: CacheReader | None = None) -> None:
        self._providers = providers
        self._cache = cache

    def eligible(self, field: str, policy: FieldPolicy) -> list[Provider]:
        """Providers that support the field and could plausibly clear the bar.

        Sorted by cost per *successful* answer, not sticker price — see
        ``Quote.expected_cost_usd``.

        Eligibility is decided on ``best_confidence``, not the typical value. A
        provider is dropped only when it could *never* clear the bar, because
        dropping it on its average throws away every above-average answer it
        would have given — and for the cheap tier those are the majority of US
        records. When a cheap source comes back under the bar, the waterfall
        escalates; that is what the waterfall is for.
        """
        usable = [
            p
            for p in self._providers
            if p.supports(field) and p.quote(field).best_confidence >= policy.min_confidence
        ]
        return sorted(usable, key=lambda p: p.quote(field).expected_cost_usd)

    async def resolve(
        self,
        *,
        field: str,
        entity_type: str,
        entity_id: str,
        subject: dict[str, Any],
        policy: FieldPolicy,
        budget_usd: float | None = None,
    ) -> Resolution:
        """Get the field as cheaply as the confidence bar allows."""
        attempts: list[Attempt] = []
        candidates = self.eligible(field, policy)
        floor = min(
            (p.quote(field).cost_usd for p in candidates),
            default=0.0,
        )

        # --- 1. the cheapest provider there is -------------------------------
        cached: CachedValue | None = None
        if self._cache is not None:
            cached = await self._cache.current(
                entity_type=entity_type, entity_id=entity_id, field=field
            )
        verdict = assess(cached, policy)
        if verdict.use_cache and verdict.cached is not None:
            attempts.append(
                Attempt(
                    provider="cache",
                    field=field,
                    outcome="skipped_cache",
                    # Costed at what we avoided, so the ledger can show savings.
                    cost_usd=floor,
                    confidence=verdict.cached.confidence,
                    note=verdict.reason,
                )
            )
            return Resolution(
                field=field,
                value=verdict.cached.value,
                confidence=verdict.cached.confidence,
                source=verdict.cached.source,
                from_cache=True,
                attempts=attempts,
            )

        # A miss is not silent. "stale:412d>90d" in the ledger is how a customer
        # later understands why a field cost them money this month.
        attempts.append(
            Attempt(provider="cache", field=field, outcome="miss", note=verdict.reason)
        )

        # --- 2. climb the waterfall, stopping at sufficiency ------------------
        best: Answer | None = None
        best_provider: str | None = None
        spent = 0.0

        for provider in candidates:
            quote = provider.quote(field)
            ceiling = _ceiling(policy, budget_usd)
            if ceiling is not None and spent + quote.cost_usd > ceiling:
                attempts.append(
                    Attempt(
                        provider=provider.name,
                        field=field,
                        outcome="skipped_budget",
                        note=f"would_exceed:{spent + quote.cost_usd:.4f}>{ceiling:.4f}",
                    )
                )
                # Providers are cost-ordered, so the next one is dearer still.
                break

            started = time.monotonic()
            try:
                answer = await provider.fetch(field, subject)
            except Exception as exc:  # a vendor being down is not our caller's problem
                attempts.append(
                    Attempt(
                        provider=provider.name,
                        field=field,
                        outcome="error",
                        latency_ms=int((time.monotonic() - started) * 1000),
                        note=type(exc).__name__,
                    )
                )
                continue

            latency = int((time.monotonic() - started) * 1000)
            spent += quote.cost_usd
            attempts.append(
                Attempt(
                    provider=provider.name,
                    field=field,
                    outcome="hit" if answer.found else "miss",
                    credits=quote.credits,
                    cost_usd=quote.cost_usd,
                    confidence=answer.confidence if answer.found else None,
                    latency_ms=latency,
                )
            )

            if answer.found and (best is None or answer.confidence > best.confidence):
                best, best_provider = answer, provider.name

            # The line that separates this from every waterfall that overspends.
            if best is not None and best.confidence >= policy.min_confidence:
                break

        if best is None or not best.found:
            return Resolution(field=field, attempts=attempts)

        return Resolution(
            field=field,
            value=best.value,
            confidence=best.confidence,
            source=best_provider,
            attempts=attempts,
        )


def _ceiling(policy: FieldPolicy, budget_usd: float | None) -> float | None:
    """The tighter of the field's own cap and the caller's remaining budget."""
    caps = [c for c in (policy.max_cost_usd, budget_usd) if c is not None]
    return min(caps) if caps else None
