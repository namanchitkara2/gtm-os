"""GTM methodology, encoded as decisions the graph can actually make.

Adapted from the GTM Strategist methodology by Maja Voje (MIT licensed, see
NOTICE). Two pieces of it earn their place here because they answer questions
the product could not previously answer.

**Account tiering** is the important one. The methodology's outbound framework
splits a target list into Tier 1 (top ~20, fully custom outreach), Tier 2 (next
~50, semi-personalised on an industry or role hook) and Tier 3 (the rest,
templated with one personalised line). That is written as advice about *writing
effort*, but it maps exactly onto this product's hardest constraint: LinkedIn
permits roughly one action at a time and ~100 a day per user, so a run cannot
afford to deep-scrape every candidate it finds.

Reading the tiers as a **budget allocator** rather than a copywriting guide is
what makes them useful here. Tier 1 earns a full profile fetch; Tiers 2 and 3
are served from what the search page already gave us for free. Before this, the
profile node fetched every candidate until it hit its cap, which spent the
scarcest resource in the system in result order rather than in fit order.

**ICP fit scoring** replaces keyword overlap. The old scorer counted how many
ICP terms appeared in a headline, which ranked a US telecom VP above the one
genuinely Bangalore-based candidate on a Bangalore search. Weighted dimensions
with a per-dimension breakdown fix the ranking and, just as importantly, give a
rationale a user can argue with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from gtmos_schemas import ICP

#: Methodology tier sizes. Tier 3 is whatever remains.
TIER_ONE_SIZE = 20
TIER_TWO_SIZE = 50

#: How much each dimension contributes to a 0-100 fit score. Location carries
#: real weight because LinkedIn's own geography filter does not work (see
#: docs/BRIDGE.md), so geography is ours to enforce and ours to rank on.
ICP_FIT_WEIGHTS: dict[str, float] = {
    "title": 40.0,
    "location": 25.0,
    "industry": 15.0,
    "keywords": 20.0,
}

#: Benchmarks the methodology gives for an outbound sequence. Held here so a
#: run can report against a published target instead of an invented one.
SEQUENCE_BENCHMARKS: dict[str, tuple[float, float]] = {
    "open_rate": (0.40, 0.60),
    "reply_rate": (0.05, 0.15),
    "positive_reply_rate": (0.02, 0.05),
}

#: Sequence shape: 8-12 touchpoints across 3-4 weeks, 4-5 of them email, and no
#: email over 150 words.
SEQUENCE_TOUCHPOINTS = (8, 12)
SEQUENCE_WEEKS = (3, 4)
MAX_EMAIL_WORDS = 150


class Tier(IntEnum):
    """Personalisation tier, which here also decides research spend."""

    ONE = 1
    TWO = 2
    THREE = 3

    @property
    def personalisation(self) -> str:
        return {
            Tier.ONE: "fully custom, referencing specifics about this account",
            Tier.TWO: "semi-personalised on an industry or role hook",
            Tier.THREE: "templated with one personalised line",
        }[self]

    @property
    def earns_profile_fetch(self) -> bool:
        """Whether this tier justifies spending a LinkedIn action on a profile.

        Only Tier 1. Fully custom outreach needs detail the search page does not
        carry; Tier 2 and 3 copy is written from title, company and location,
        all of which the search results already gave us for nothing.
        """
        return self is Tier.ONE


@dataclass(frozen=True)
class FitBreakdown:
    """A fit score and the reason for it, dimension by dimension."""

    score: float
    dimensions: dict[str, float] = field(default_factory=dict)
    matched: dict[str, list[str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def rationale(self) -> str:
        parts = [
            f"{name} {value:.0f}/{ICP_FIT_WEIGHTS[name]:.0f}"
            for name, value in sorted(self.dimensions.items(), key=lambda kv: -kv[1])
        ]
        hits = ", ".join(sorted({v for values in self.matched.values() for v in values}))
        text = "; ".join(parts)
        if hits:
            text += f" (matched: {hits})"
        if self.missing:
            text += f"; no signal on: {', '.join(self.missing)}"
        return text


def _overlap(terms: list[str], haystack: str) -> list[str]:
    lowered = haystack.lower()
    return sorted({t.strip().lower() for t in terms if t.strip() and t.strip().lower() in lowered})


def score_icp_fit(
    icp: ICP,
    *,
    headline: str | None,
    location: str | None,
    location_matches: bool | None,
    company: str | None = None,
) -> FitBreakdown:
    """Score one person against an ICP, dimension by dimension.

    ``location_matches`` is passed in rather than recomputed because geography
    is resolved upstream, at the search step, where rejecting a candidate is
    still free.
    """
    text = " ".join(filter(None, [headline, company])) or ""
    dimensions: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    missing: list[str] = []

    title_hits = _overlap(icp.titles, text)
    if icp.titles:
        dimensions["title"] = ICP_FIT_WEIGHTS["title"] * (len(title_hits) / len(icp.titles))
        if title_hits:
            matched["title"] = title_hits
    elif text:
        # No titles asked for: do not punish the candidate for it. Award the
        # dimension so scores stay comparable across differently specified ICPs.
        dimensions["title"] = ICP_FIT_WEIGHTS["title"]

    if not icp.locations:
        dimensions["location"] = ICP_FIT_WEIGHTS["location"]
    elif location_matches is True:
        dimensions["location"] = ICP_FIT_WEIGHTS["location"]
        if location:
            matched["location"] = [location]
    elif location_matches is None:
        # Unknown is not wrong. Half credit, and it is reported as unknown so a
        # user can see the difference between "matches" and "we could not tell".
        dimensions["location"] = ICP_FIT_WEIGHTS["location"] / 2
        missing.append("location")
    else:
        dimensions["location"] = 0.0

    industry_hits = _overlap(icp.industries, text)
    if icp.industries:
        dimensions["industry"] = ICP_FIT_WEIGHTS["industry"] * (
            len(industry_hits) / len(icp.industries)
        )
        if industry_hits:
            matched["industry"] = industry_hits
    else:
        dimensions["industry"] = ICP_FIT_WEIGHTS["industry"]

    keyword_hits = _overlap(icp.keywords, text)
    if icp.keywords:
        dimensions["keywords"] = ICP_FIT_WEIGHTS["keywords"] * (
            len(keyword_hits) / len(icp.keywords)
        )
        if keyword_hits:
            matched["keywords"] = keyword_hits
    else:
        dimensions["keywords"] = ICP_FIT_WEIGHTS["keywords"]

    if not text:
        missing.append("headline")

    return FitBreakdown(
        score=round(sum(dimensions.values()), 1),
        dimensions={k: round(v, 1) for k, v in dimensions.items()},
        matched=matched,
        missing=missing,
    )


@dataclass(frozen=True)
class TierPlan:
    """Who lands in which tier, and how many LinkedIn actions that costs."""

    tiers: dict[str, Tier]
    profile_fetches: list[str]
    budget_capped: bool = False

    def tier_of(self, key: str) -> Tier:
        return self.tiers.get(key, Tier.THREE)

    @property
    def counts(self) -> dict[int, int]:
        counts = {1: 0, 2: 0, 3: 0}
        for tier in self.tiers.values():
            counts[int(tier)] += 1
        return counts


def assign_tiers(
    ranked_keys: list[str],
    *,
    bridge_budget: int | None = None,
    tier_one_size: int = TIER_ONE_SIZE,
    tier_two_size: int = TIER_TWO_SIZE,
) -> TierPlan:
    """Split a fit-ranked list into tiers and decide where research is spent.

    ``ranked_keys`` must already be ordered best-fit first; tiering is an
    allocation decision, not a scoring one.

    ``bridge_budget`` caps Tier 1, because Tier 1 is the only tier that spends
    LinkedIn actions. A run with 6 calls left cannot have 20 fully-custom
    accounts, and silently promoting 20 of them would produce a plan the run
    then fails to execute. Capping is recorded on the plan so the trace can say
    the tier was shaped by budget rather than by fit.
    """
    effective_one = tier_one_size
    capped = False
    if bridge_budget is not None and bridge_budget < effective_one:
        effective_one = max(0, bridge_budget)
        capped = True

    tiers: dict[str, Tier] = {}
    for index, key in enumerate(ranked_keys):
        if index < effective_one:
            tiers[key] = Tier.ONE
        elif index < effective_one + tier_two_size:
            tiers[key] = Tier.TWO
        else:
            tiers[key] = Tier.THREE

    return TierPlan(
        tiers=tiers,
        profile_fetches=[k for k, t in tiers.items() if t.earns_profile_fetch],
        budget_capped=capped,
    )
