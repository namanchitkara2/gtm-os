"""Provenance for every value the platform reports.

The rule this module exists to enforce: no field reaches a customer without an
answer to "where did that come from, when, and how sure are we". Enrichment
waterfalls are guessing machines — Apollo says one email, a scraped team page
says another — and a row that has lost track of which source said what cannot be
audited, deduped by trust, or defended when a customer says "this bounced".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

#: How a value was obtained. ``inference`` covers anything a model produced
#: rather than read, which is the class of value that must never be presented
#: with the same weight as a scraped or API-returned one.
Method = Literal["scrape", "api", "inference", "user"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Evidence(BaseModel):
    """One source's claim about one value."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(description="linkedin | apollo | clay | hunter | web | inference")
    method: Method
    source_url: str | None = None
    retrieved_at: datetime = Field(default_factory=_utcnow)
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = None


class Sourced(BaseModel, Generic[T]):
    """A value plus every source that supports it.

    Multiple pieces of evidence are the normal case, not the exception: two
    providers agreeing is the strongest signal available short of a verified
    send, and collapsing them to one loses exactly that.
    """

    value: T
    #: Sources that back *this* value. Only these count toward confidence.
    evidence: list[Evidence] = Field(default_factory=list)
    #: Sources that claimed a *different* value for the same field. Retained
    #: rather than dropped: a provider disagreement is the strongest available
    #: signal that a row is wrong, and it is invisible if the loser is deleted.
    conflicts: list[Evidence] = Field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Confidence in this value: lifted by agreement, cut by contradiction.

        Two parts, because two different things are being measured.

        *Agreement* — each extra provider naming the same value closes half the
        remaining gap to certainty. Independent corroboration is the strongest
        signal available short of a verified send, and it never reaches 1.0.

        *Contradiction* — providers that named a different value are evidence
        against, weighted by how confident each of them was. A field two vendors
        disagree about is genuinely less trustworthy than one a single vendor
        reported unopposed, so a contradicted value can and should fall below
        its own uncontradicted single-source figure. An early version counted
        dissenters as corroboration and reported 0.9 on a flat disagreement;
        the one after it applied a flat penalty and let a weak 0.2 dissenter
        cost as much as a confident 0.9 one.
        """
        if not self.evidence:
            return 0.0

        def mass(items: list[Evidence]) -> float:
            """Total weight of distinct providers, at each one's best claim."""
            best: dict[str, float] = {}
            for e in items:
                best[e.provider] = max(best.get(e.provider, 0.0), e.confidence)
            return sum(best.values())

        lifted = max(e.confidence for e in self.evidence)
        for _ in range(len({e.provider for e in self.evidence}) - 1):
            lifted += (1.0 - lifted) / 2

        supporters = {e.provider for e in self.evidence}
        dissent = mass([e for e in self.conflicts if e.provider not in supporters])
        if dissent:
            lifted *= mass(self.evidence) / (mass(self.evidence) + dissent)

        return round(min(lifted, 0.99), 4)

    @property
    def has_conflict(self) -> bool:
        """Whether any source named a different value. Surface this in the UI."""
        return bool(self.conflicts)

    @property
    def is_inferred(self) -> bool:
        """True when nothing but a model supports this value."""
        return bool(self.evidence) and all(e.method == "inference" for e in self.evidence)

    @classmethod
    def of(
        cls,
        value: T,
        *,
        provider: str,
        method: Method,
        confidence: float,
        source_url: str | None = None,
        note: str | None = None,
    ) -> Sourced[T]:
        """Build a single-source value. The common case at ingestion time."""
        return cls(
            value=value,
            evidence=[
                Evidence(
                    provider=provider,
                    method=method,
                    confidence=confidence,
                    source_url=source_url,
                    note=note,
                )
            ],
        )

    def merged_with(self, other: Sourced[T]) -> Sourced[T]:
        """Combine two claims about the same field.

        Keeps the value backed by the highest single-source confidence and
        retains *both* evidence trails, including the losing one — a
        disagreement is a fact about the record and deleting it hides the only
        signal that something is wrong.
        """
        if self.value == other.value:
            return Sourced[T](
                value=self.value,
                evidence=[*self.evidence, *other.evidence],
                conflicts=[*self.conflicts, *other.conflicts],
            )
        keep, drop = (self, other) if self.confidence >= other.confidence else (other, self)
        return Sourced[T](
            value=keep.value,
            evidence=list(keep.evidence),
            conflicts=[*keep.conflicts, *drop.evidence, *drop.conflicts],
        )
