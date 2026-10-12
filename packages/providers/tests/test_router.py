"""What the router must never do, expressed as tests.

Three failures this package exists to prevent, in order of how much money they
cost: paying for something already held, climbing past the point of sufficiency,
and routing to whichever vendor is cheapest at saying "no".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gtmos_providers import Answer, FieldPolicy, Quote, Router
from gtmos_providers.cache import CachedValue, assess


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeProvider:
    """A provider with a known price and a scripted answer."""

    def __init__(
        self,
        name: str,
        *,
        cost_usd: float,
        confidence: float,
        found: bool = True,
        hit_rate: float = 1.0,
        raises: bool = False,
    ) -> None:
        self.name = name
        self._cost = cost_usd
        self._confidence = confidence
        self._found = found
        self._hit_rate = hit_rate
        self._raises = raises
        self.calls = 0

    def supports(self, field: str) -> bool:
        return True

    def quote(self, field: str) -> Quote:
        return Quote(
            credits=self._cost * 50,
            cost_usd=self._cost,
            typical_confidence=self._confidence,
            hit_rate=self._hit_rate,
        )

    async def fetch(self, field: str, subject: dict[str, Any]) -> Answer:
        self.calls += 1
        if self._raises:
            raise RuntimeError("vendor down")
        if not self._found:
            return Answer()
        return Answer(value=f"{self.name}-value", confidence=self._confidence)


class FakeCache:
    def __init__(self, value: CachedValue | None = None) -> None:
        self._value = value

    async def current(self, *, entity_type: str, entity_id: str, field: str) -> CachedValue | None:
        return self._value


POLICY = FieldPolicy(field="email", ttl_days=180, min_confidence=0.8)


async def _resolve(router: Router, policy: FieldPolicy = POLICY):
    return await router.resolve(
        field="email",
        entity_type="person",
        entity_id="11111111-1111-1111-1111-111111111111",
        subject={"full_name": "Test Person"},
        policy=policy,
    )


@pytest.mark.asyncio
async def test_fresh_cache_spends_nothing():
    cheap = FakeProvider("cheap", cost_usd=0.04, confidence=0.9)
    cache = FakeCache(
        CachedValue(value="a@b.com", confidence=0.9, observed_at=_now() - timedelta(days=5),
                    source="apollo")
    )
    result = await _resolve(Router([cheap], cache))

    assert result.value == "a@b.com"
    assert result.from_cache is True
    assert cheap.calls == 0
    assert result.spend_usd == 0.0
    # The avoided cost is reported, so the cache is visible as a saving rather
    # than as the silent absence of a charge.
    assert result.saved_usd == 0.04


@pytest.mark.asyncio
async def test_stale_cache_is_not_served_silently():
    cheap = FakeProvider("cheap", cost_usd=0.04, confidence=0.9)
    cache = FakeCache(
        CachedValue(value="old@b.com", confidence=0.95,
                    observed_at=_now() - timedelta(days=400), source="apollo")
    )
    result = await _resolve(Router([cheap], cache))

    assert result.value == "cheap-value"
    assert cheap.calls == 1
    assert any(a.outcome == "miss" and "stale" in (a.note or "") for a in result.attempts)


@pytest.mark.asyncio
async def test_stops_at_sufficient_confidence():
    """The line that separates this from a waterfall that overspends."""
    cheap = FakeProvider("cheap", cost_usd=0.04, confidence=0.85)
    dear = FakeProvider("dear", cost_usd=0.24, confidence=0.95)
    result = await _resolve(Router([cheap, dear], FakeCache()))

    assert result.source == "cheap"
    assert dear.calls == 0, "premium tier called after the bar was already met"
    assert result.spend_usd == 0.04


@pytest.mark.asyncio
async def test_escalates_when_cheap_source_misses():
    cheap = FakeProvider("cheap", cost_usd=0.04, confidence=0.85, found=False)
    dear = FakeProvider("dear", cost_usd=0.24, confidence=0.95)
    result = await _resolve(Router([cheap, dear], FakeCache()))

    assert result.source == "dear"
    assert cheap.calls == 1 and dear.calls == 1
    assert result.spend_usd == pytest.approx(0.28)


@pytest.mark.asyncio
async def test_orders_by_cost_per_successful_answer_not_sticker_price():
    """A 2-credit source that answers 20% of the time is not cheap."""
    lottery = FakeProvider("lottery", cost_usd=0.04, confidence=0.9, hit_rate=0.2)
    reliable = FakeProvider("reliable", cost_usd=0.16, confidence=0.9, hit_rate=0.9)
    order = [p.name for p in Router([lottery, reliable], FakeCache()).eligible("email", POLICY)]

    assert order == ["reliable", "lottery"]


@pytest.mark.asyncio
async def test_provider_below_the_bar_is_never_called():
    """Calling it could only ever return a value we would refuse to use."""
    weak = FakeProvider("weak", cost_usd=0.01, confidence=0.5)
    ok = FakeProvider("ok", cost_usd=0.20, confidence=0.9)
    result = await _resolve(Router([weak, ok], FakeCache()))

    assert weak.calls == 0
    assert result.source == "ok"


@pytest.mark.asyncio
async def test_budget_ceiling_stops_the_climb_and_says_so():
    cheap = FakeProvider("cheap", cost_usd=0.04, confidence=0.85, found=False)
    dear = FakeProvider("dear", cost_usd=0.90, confidence=0.95)
    router = Router([cheap, dear], FakeCache())
    result = await router.resolve(
        field="email",
        entity_type="person",
        entity_id="11111111-1111-1111-1111-111111111111",
        subject={},
        policy=POLICY,
        budget_usd=0.10,
    )

    assert result.found is False
    assert dear.calls == 0
    assert any(a.outcome == "skipped_budget" for a in result.attempts)


@pytest.mark.asyncio
async def test_vendor_error_is_recorded_and_the_waterfall_continues():
    broken = FakeProvider("broken", cost_usd=0.04, confidence=0.9, raises=True)
    ok = FakeProvider("ok", cost_usd=0.20, confidence=0.9)
    result = await _resolve(Router([broken, ok], FakeCache()))

    assert result.source == "ok"
    assert any(a.outcome == "error" and a.provider == "broken" for a in result.attempts)
    # An error costs nothing: we do not charge a customer for a vendor's outage.
    assert result.spend_usd == 0.20


@pytest.mark.asyncio
async def test_every_path_leaves_a_ledger_trail():
    result = await _resolve(Router([FakeProvider("p", cost_usd=0.04, confidence=0.9)], FakeCache()))
    assert result.attempts, "a resolution with no attempts cannot be audited or billed"
    assert result.attempts[0].provider == "cache"


def test_low_confidence_cache_escalates_rather_than_reusing():
    verdict = assess(
        CachedValue(value="x", confidence=0.4, observed_at=_now(), source="inference"),
        POLICY,
    )
    assert verdict.use_cache is False
    assert "low_confidence" in verdict.reason


def test_missing_cache_reports_why():
    assert assess(None, POLICY).reason == "not_cached"


@pytest.mark.asyncio
async def test_cheap_tier_is_tried_even_when_its_average_is_below_the_bar():
    """The bug this caught in the live product: eligibility on an average.

    Apollo's email confidence is ~88% in the US and 60-73% elsewhere, so its
    blended typical sits under a 0.8 bar. Filtering on that sent every single
    email lookup straight to a source costing four times as much. Eligibility
    belongs on what a provider can do at its best; the waterfall handles the
    times it falls short.
    """
    class Tiered(FakeProvider):
        def __init__(self, name, *, cost_usd, typical, ceiling, answers_at):
            super().__init__(name, cost_usd=cost_usd, confidence=typical)
            self._ceiling = ceiling
            self._answers_at = answers_at

        def quote(self, field):
            return Quote(
                cost_usd=self._cost,
                typical_confidence=self._confidence,
                ceiling_confidence=self._ceiling,
                hit_rate=1.0,
            )

        async def fetch(self, field, subject):
            self.calls += 1
            return Answer(value=f"{self.name}-value", confidence=self._answers_at)

    # Blended 0.75, but reaches 0.88 on a good record — and this is a good one.
    cheap = Tiered("apollo", cost_usd=0.04, typical=0.75, ceiling=0.88, answers_at=0.85)
    dear = Tiered("premium", cost_usd=0.16, typical=0.90, ceiling=0.95, answers_at=0.93)

    result = await _resolve(Router([cheap, dear], FakeCache()))

    assert cheap.calls == 1, "the cheap tier must be tried first"
    assert dear.calls == 0, "and must not be escalated past once it clears the bar"
    assert result.spend_usd == 0.04


@pytest.mark.asyncio
async def test_cheap_answer_below_the_bar_still_escalates():
    """The other half: trying cheap first must not mean accepting cheap."""
    class Weak(FakeProvider):
        def quote(self, field):
            return Quote(cost_usd=self._cost, typical_confidence=self._confidence,
                         ceiling_confidence=0.9, hit_rate=1.0)

        async def fetch(self, field, subject):
            self.calls += 1
            return Answer(value="weak-value", confidence=0.6)   # under the 0.8 bar

    weak = Weak("apollo", cost_usd=0.04, confidence=0.75)
    strong = FakeProvider("premium", cost_usd=0.16, confidence=0.93)
    result = await _resolve(Router([weak, strong], FakeCache()))

    assert weak.calls == 1 and strong.calls == 1
    assert result.source == "premium"
