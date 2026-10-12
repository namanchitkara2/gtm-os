"""What a provider is, and what it costs to ask it something.

Every enrichment source in the product — a paid API, a scrape, the local cache,
a human typing a value in — implements the same three-method shape. The router
never special-cases a vendor, because the moment it does, "which source is
cheapest for this field" stops being a decision the system can make and becomes
a decision buried in an if-statement.

The cost numbers here are the point of the package. Providers charge wildly
different amounts for the same field: inside Clay, an Apollo lookup costs 1-2
credits against ZoomInfo's 5-10 for the same email, and Apollo's own calculator
prices an email at 2 credits and a phone at 8 (docs/product-research.md). A 5-10x
spread on an identical field is a routing problem with a correct answer, and
nobody in the market sells the router because every vendor in that list is paid
by the lookup.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

#: Fields the router knows how to shop for. Deliberately small: each one needs a
#: freshness policy and at least two sources before it is worth routing at all.
Field_ = Literal[
    "email",
    "phone",
    "title",
    "company",
    "headcount",
    "tech_stack",
    "linkedin_url",
]

#: Why an attempt ended. ``skipped_cache`` and ``skipped_budget`` are successes
#: from the product's point of view — they are the router declining to spend —
#: and they are recorded with the same weight as a hit so the savings are
#: visible in the ledger rather than invisible in the absence of a charge.
Outcome = Literal["hit", "miss", "error", "skipped_cache", "skipped_budget"]


class Quote(BaseModel):
    """What a provider expects an attempt to cost before it is made.

    A quote is not a promise. It is what the router sorts on, so it has to be
    honest about the *typical* case rather than the best one: a provider that
    quotes optimistically wins every auction and then misses.
    """

    model_config = ConfigDict(frozen=True)

    credits: float = Field(default=0.0, ge=0.0, description="In the provider's own unit")
    cost_usd: float = Field(default=0.0, ge=0.0)
    #: What this provider usually returns for this field when it returns
    #: anything. An average, used for ordering.
    typical_confidence: float = Field(ge=0.0, le=1.0)
    #: The best this provider ever does for this field. Defaults to the typical
    #: value, and exists because eligibility must not be decided on an average:
    #: Apollo's email confidence is ~88% in the US and 60-73% elsewhere, so a
    #: filter on the blended number excludes the cheap tier from every US lookup
    #: and quietly routes the most common field in the product to a source
    #: costing four times as much.
    ceiling_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Probability the call returns a value at all. A cheap provider that finds
    #: nothing 80% of the time is not cheap.
    hit_rate: float = Field(default=0.5, ge=0.0, le=1.0)

    @property
    def best_confidence(self) -> float:
        """What this provider could return at its best. Eligibility uses this."""
        return self.ceiling_confidence if self.ceiling_confidence is not None else self.typical_confidence

    @property
    def expected_cost_usd(self) -> float:
        """Cost per *successful* answer, which is what actually matters.

        Sorting on sticker price alone routes to whichever vendor is cheapest at
        saying "no". Dividing by hit rate is what makes a 2-credit source with a
        20% hit rate correctly lose to an 8-credit source that answers.
        """
        if self.hit_rate <= 0:
            return float("inf")
        return self.cost_usd / self.hit_rate


class Answer(BaseModel):
    """One provider's response about one field."""

    model_config = ConfigDict(frozen=True)

    value: Any | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_url: str | None = None
    #: Free-form, provider-specific. Kept out of ``value`` so a change in a
    #: vendor's payload shape can never silently change what we assert.
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.value is not None


class Attempt(BaseModel):
    """A single row of the enrichment ledger, before it is written.

    Every path through the router produces one of these — including the paths
    where nothing was spent. The ledger is both the cost report and the training
    data for reordering the waterfall later (roadmap E4), and a ledger that only
    records spending cannot tell you what the cache saved you.
    """

    provider: str
    field: str
    outcome: Outcome
    credits: float = 0.0
    cost_usd: float = 0.0
    confidence: float | None = None
    latency_ms: int | None = None
    note: str | None = None


@runtime_checkable
class Provider(Protocol):
    """A source of field values.

    Implementations must be side-effect free apart from the network call and must
    never raise for a miss: "not found" is an ordinary answer and a router that
    has to catch exceptions to tell miss from error cannot report either.
    """

    name: str

    def supports(self, field: str) -> bool:
        """Whether this provider can be asked for this field at all."""
        ...

    def quote(self, field: str) -> Quote:
        """Expected cost and confidence, before the call."""
        ...

    async def fetch(self, field: str, subject: dict[str, Any]) -> Answer:
        """Ask. ``subject`` carries whatever identifiers are known so far."""
        ...
