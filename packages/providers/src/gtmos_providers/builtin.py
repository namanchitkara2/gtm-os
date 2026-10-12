"""Concrete providers, and the price list the router sorts on.

The numbers below are the published ones as of September 2026
(docs/product-research.md): Apollo prices an email at 2 credits and a phone at 8
on its own enrichment calculator, and inside Clay an Apollo lookup runs 1-2
credits against ZoomInfo's 5-10 for the same field. Clay's own tiers work out
near $0.0185/credit at Launch ($185 for 10k) and $0.0198 at Growth ($495 for
25k), so a cent-ish per credit is the right order of magnitude to route on.

These are *quotes*, not invoices. They exist so the router can order sources
before it calls them, and they are deliberately kept in one readable place
rather than scattered through adapters, because the day a vendor changes its
pricing this file is the diff.

Hit rates carry the regional truth the vendors do not advertise: Apollo's
accuracy is about 88% in the US but **60-73% internationally**, and users report
15-35% bounce rates on exported lists even on contacts marked verified. A router
that treats "verified" as 1.0 will route to the cheapest liar every time.
"""

from __future__ import annotations

from typing import Any

from .base import Answer, Provider, Quote

#: Rough blended price of one credit in USD, from Clay's 2026 tiers. Used only
#: to turn credit quotes into a comparable number.
CREDIT_USD = 0.02


class StaticQuoteProvider:
    """Base for adapters whose prices are a published table.

    Subclasses supply the table and the network call. Everything else — support
    checks, quoting, the shape of a miss — is identical across vendors and
    should not be reimplemented per adapter.
    """

    name = "static"
    #: field -> (credits, typical_confidence, hit_rate, ceiling_confidence)
    #: The ceiling is what the source achieves on its best records — a US
    #: contact at a well-known company — and it is what decides eligibility.
    prices: dict[str, tuple[float, float, float, float]] = {}

    def supports(self, field: str) -> bool:
        return field in self.prices

    def quote(self, field: str) -> Quote:
        credits, confidence, hit_rate, ceiling = self.prices[field]
        return Quote(
            credits=credits,
            cost_usd=round(credits * CREDIT_USD, 6),
            typical_confidence=confidence,
            ceiling_confidence=ceiling,
            hit_rate=hit_rate,
        )

    async def fetch(self, field: str, subject: dict[str, Any]) -> Answer:  # pragma: no cover
        raise NotImplementedError


class ApolloProvider(StaticQuoteProvider):
    """Cheap, broad, and weakest exactly where the user is not American.

    The hit rates encode the regional story rather than the marketing number:
    high for a US email, materially lower for anything outside it. Phones are
    priced at 8 credits against an email's 2, which is why a workflow that asks
    for a phone "just in case" is four times more expensive than one that asks
    when it needs one.
    """

    name = "apollo"
    prices = {
        "email": (2.0, 0.75, 0.70, 0.88),
        "phone": (8.0, 0.70, 0.45, 0.85),
        "title": (1.0, 0.70, 0.80, 0.88),
        "company": (1.0, 0.80, 0.85, 0.92),
        "headcount": (1.0, 0.75, 0.80, 0.88),
        "linkedin_url": (1.0, 0.85, 0.75, 0.95),
    }

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    async def fetch(self, field: str, subject: dict[str, Any]) -> Answer:
        if self._client is None:
            return Answer()
        return await self._client.enrich(field=field, subject=subject)


class PremiumProvider(StaticQuoteProvider):
    """The escalation tier: 5-10x the price, used only when cheap was not enough.

    Modelled on ZoomInfo/Cognism-class sources. It exists in the waterfall so the
    router has somewhere to go when confidence falls short — and it stays unused
    on the majority of records, which is the entire economic argument.
    """

    name = "premium"
    prices = {
        "email": (8.0, 0.90, 0.85, 0.95),
        "phone": (12.0, 0.88, 0.70, 0.94),
        "title": (5.0, 0.90, 0.90, 0.95),
        "company": (5.0, 0.92, 0.92, 0.97),
        "headcount": (5.0, 0.88, 0.90, 0.95),
        "tech_stack": (6.0, 0.80, 0.75, 0.90),
    }

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    async def fetch(self, field: str, subject: dict[str, Any]) -> Answer:
        if self._client is None:
            return Answer()
        return await self._client.enrich(field=field, subject=subject)


class BridgeProvider(StaticQuoteProvider):
    """The user's own LinkedIn session, via the local bridge.

    Priced at zero dollars and quoted as the most trustworthy source there is,
    because it reads the profile the person actually maintains. That combination
    would make the router pick it every time, which is exactly wrong: the real
    cost is not money, it is one of roughly a hundred actions this user has for
    the whole day, and spending them on enrichment starves the sourcing they
    asked for.

    So this provider is **not registered in the default waterfall**. It is opt-in,
    for callers that have already decided a profile read is worth an action — and
    keeping that decision outside the cost router is deliberate, because a
    cheapest-first sort will always reach for a free source and no amount of
    tuning fixes that.
    """

    name = "bridge"
    prices = {
        "title": (0.0, 0.95, 0.95, 0.99),
        "company": (0.0, 0.95, 0.95, 0.99),
        "headcount": (0.0, 0.85, 0.60, 0.95),
        "linkedin_url": (0.0, 0.99, 0.99, 0.99),
    }

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    async def fetch(self, field: str, subject: dict[str, Any]) -> Answer:
        if self._client is None:
            return Answer()
        return await self._client.read_profile(field=field, subject=subject)


def default_waterfall(*, apollo: Any | None = None, premium: Any | None = None) -> list[Provider]:
    """The providers a tenant gets before it configures anything.

    Cheap first is not hardcoded here — the router sorts by expected cost, so
    this is just the set of things it is allowed to consider. The bridge is
    absent on purpose; see ``BridgeProvider``.
    """
    return [ApolloProvider(apollo), PremiumProvider(premium)]
