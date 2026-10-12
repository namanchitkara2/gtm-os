"""Cache-first, cost-aware enrichment routing.

    from gtmos_providers import Router, FieldPolicy, default_waterfall

    router = Router(default_waterfall(apollo=client), cache=repo)
    result = await router.resolve(
        field="email",
        entity_type="person",
        entity_id=person_id,
        subject={"full_name": ..., "company": ...},
        policy=FieldPolicy(field="email", ttl_days=180, min_confidence=0.8),
    )
    result.value, result.spend_usd, result.attempts   # -> enrichment_ledger

See docs/architecture-decisions.md ADR-008 for why this is foundation rather
than optimisation.
"""

from .base import Answer, Attempt, Outcome, Provider, Quote
from .builtin import ApolloProvider, BridgeProvider, PremiumProvider, default_waterfall
from .cache import CachedValue, CacheReader, FieldPolicy, Verdict, assess, stale_after
from .router import Resolution, Router

__all__ = [
    "Answer",
    "ApolloProvider",
    "Attempt",
    "BridgeProvider",
    "CacheReader",
    "CachedValue",
    "FieldPolicy",
    "Outcome",
    "PremiumProvider",
    "Provider",
    "Quote",
    "Resolution",
    "Router",
    "Verdict",
    "assess",
    "default_waterfall",
    "stale_after",
]
