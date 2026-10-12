"""Domain policy and the rate limits that stop one tenant taking the whole pool.

Two separate concerns that are easy to confuse:

* **Politeness** is per *domain*, and it is not ours to spend. A site's crawl-delay
  applies no matter which of our customers triggered the fetch, so the bucket is
  keyed on the domain alone. Ten tenants scraping the same site must share one
  rate, or the site sees a coordinated burst and blocks all of them.

* **Fairness** is per *workspace*, and it exists so one customer's 10,000-URL job
  cannot starve everyone else. Keyed on the tenant, independent of domain.

Both must pass. A request that is polite to the domain but exhausts the tenant's
share is still refused, and vice versa — which is why :meth:`Limiter.acquire`
checks every level rather than the tightest one.

On robots.txt: the policy carries the directive and the crawl delay, and
``respect_robots`` defaults to True. This module does not implement fetching or
parsing robots.txt (that needs the network); it is the place the parsed result
lands, and :meth:`DomainPolicy.may_fetch` is the single question the orchestrator
asks. Nothing here attempts to work around a block — a domain that disallows us is
marked ``blocked`` and the job fails with a reason the user can read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

#: Applied when a domain has no specific policy. One request per second with two
#: in flight is slower than most crawlers default to, deliberately: the cost of
#: being too slow is latency, and the cost of being too fast is a blocked IP and a
#: customer whose job cannot complete at all.
DEFAULT_RPS = 1.0
DEFAULT_CONCURRENCY = 2


class Fetcher(StrEnum):
    """Which strategy to use. Ordered cheapest-first, and that order is the policy.

    A browser costs roughly 50-100x an HTTP fetch in CPU and memory and is the
    difference between one worker handling hundreds of pages a minute and handling
    a handful. It is reserved for pages that provably need it.
    """

    CACHED = "cached"
    HTTP = "http"
    BROWSER = "browser"
    SEARCH = "search"


class RobotsPolicy(StrEnum):
    RESPECT = "respect"
    #: For a domain the tenant owns and has authorised us to crawl. Requires an
    #: explicit per-domain setting; there is no global override.
    OWNED_BY_TENANT = "owned_by_tenant"


@dataclass
class DomainPolicy:
    """How to treat one domain. Config-driven; nothing here is hardcoded at a call site."""

    domain: str
    requests_per_second: float = DEFAULT_RPS
    concurrent_requests: int = DEFAULT_CONCURRENCY
    robots_policy: RobotsPolicy = RobotsPolicy.RESPECT
    preferred_fetcher: Fetcher = Fetcher.HTTP
    timeout_s: float = 15.0
    max_attempts: int = 3
    #: From the site's robots.txt, when it specifies one. Takes precedence over
    #: ``requests_per_second`` whenever it is slower — a published crawl-delay is
    #: the site telling us its limit, and our default is only a guess.
    crawl_delay_s: float | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    #: Paths robots.txt disallows, as prefixes.
    disallowed_paths: tuple[str, ...] = ()
    #: How long a fetched page stays fresh. Per-domain because the right answer
    #: differs by an order of magnitude between a careers page and an about page.
    freshness_s: int = 30 * 86400

    @property
    def effective_interval_s(self) -> float:
        """Minimum seconds between requests, honouring the slower of the two limits."""
        own = 1.0 / self.requests_per_second if self.requests_per_second > 0 else float("inf")
        if self.crawl_delay_s is not None:
            return max(own, self.crawl_delay_s)
        return own

    def may_fetch(self, path: str) -> tuple[bool, str | None]:
        """Whether this path may be fetched at all, and why not.

        A disallowed path is a refusal, not a slower fetch. There is deliberately
        no bypass: when a site says no, the correct behaviour is to fail with a
        reason and let the user reach for an authorised API instead.
        """
        if self.blocked:
            return False, self.blocked_reason or f"{self.domain} is blocked"
        if self.robots_policy is RobotsPolicy.OWNED_BY_TENANT:
            return True, None
        for prefix in self.disallowed_paths:
            if path.startswith(prefix):
                return False, f"robots.txt disallows {prefix} on {self.domain}"
        return True, None


#: Freshness by the kind of page, not by domain. Configuration rather than
#: constants scattered through extractors: a job posting goes stale in hours while
#: an about page is good for a month, and the difference is worth real money in
#: avoided fetches.
FRESHNESS_BY_KIND: dict[str, int] = {
    "job_posting": 6 * 3600,
    "careers_index": 12 * 3600,
    "leadership_page": 7 * 86400,
    "team_page": 7 * 86400,
    "pricing_page": 14 * 86400,
    "company_website": 30 * 86400,
    "about_page": 30 * 86400,
    "press_release": 90 * 86400,
    "contact_page": 30 * 86400,
}


class RateLimited(Exception):
    """Capacity is not available. Carries how long to wait rather than just failing.

    ``retry_after_s`` is what makes this actionable: a queue that knows when a slot
    frees up can reschedule the job instead of spinning on it, which is the
    difference between backpressure and a busy loop.
    """

    def __init__(self, level: str, key: str, retry_after_s: float) -> None:
        self.level = level
        self.key = key
        self.retry_after_s = retry_after_s
        super().__init__(f"{level} limit reached for {key}; retry in {retry_after_s:.2f}s")


@dataclass
class _Bucket:
    """A token bucket. Refills continuously rather than on a tick.

    Continuous refill matters for politeness: a bucket that refills in steps lets
    a burst of N requests leave at the same instant, which is exactly the traffic
    shape a site's rate detector looks for.
    """

    rate_per_s: float
    capacity: float
    tokens: float = field(default=0.0)
    updated_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens == 0.0:
            self.tokens = self.capacity

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated_at
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_s)
            self.updated_at = now

    def take(self, now: float | None = None) -> float:
        """Consume a token, or return the seconds until one is available."""
        now = time.monotonic() if now is None else now
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        deficit = 1.0 - self.tokens
        return deficit / self.rate_per_s if self.rate_per_s > 0 else float("inf")


class Limiter:
    """Every rate limit in the scraping subsystem, checked in one place.

    Levels, in the order the request description specifies them: global, workspace,
    provider, domain. All must pass. Concurrency is tracked separately from rate
    because they fail differently — a rate limit clears with time, a concurrency
    limit clears only when an in-flight request finishes.

    Not thread-safe by design: it is driven from one asyncio event loop per worker
    process, and adding a lock would imply a cross-process guarantee it cannot
    make. Multi-process deployment needs this state in Redis or Postgres, and
    :meth:`snapshot` exists so that move does not require reshaping the callers.
    """

    def __init__(
        self,
        *,
        global_rps: float = 20.0,
        workspace_rps: float = 5.0,
        global_concurrency: int = 32,
        workspace_concurrency: int = 8,
    ) -> None:
        self._global = _Bucket(global_rps, max(global_rps, 1.0))
        self._workspace_rps = workspace_rps
        self._workspace: dict[str, _Bucket] = {}
        self._domain: dict[str, _Bucket] = {}
        self._provider: dict[str, _Bucket] = {}
        self._global_concurrency = global_concurrency
        self._workspace_concurrency = workspace_concurrency
        self._inflight_global = 0
        self._inflight_workspace: dict[str, int] = {}
        self._inflight_domain: dict[str, int] = {}

    def _workspace_bucket(self, tenant: str) -> _Bucket:
        if tenant not in self._workspace:
            self._workspace[tenant] = _Bucket(self._workspace_rps, max(self._workspace_rps, 1.0))
        return self._workspace[tenant]

    def _domain_bucket(self, policy: DomainPolicy) -> _Bucket:
        key = policy.domain
        interval = policy.effective_interval_s
        rate = 1.0 / interval if interval > 0 else 0.0
        bucket = self._domain.get(key)
        # A policy change (a newly discovered crawl-delay) must take effect without
        # discarding the tokens already spent, or a reload becomes a way to burst.
        if bucket is None:
            # Capacity 1, deliberately: no burst. Sizing the bucket to the
            # concurrency limit lets N requests leave in the same instant, which
            # is the exact traffic shape a site's rate detector fires on — and it
            # made a shared-domain limit pass for five tenants in a row before a
            # test caught it. Concurrency bounds how many run at once; this bounds
            # how closely spaced they are, and the two must not be conflated.
            bucket = self._domain[key] = _Bucket(rate, 1.0)
        elif bucket.rate_per_s != rate:
            bucket.rate_per_s = rate
        return bucket

    def check(self, *, tenant_id: str, policy: DomainPolicy, provider: str | None = None) -> None:
        """Raise :class:`RateLimited` if any level is out of capacity.

        Concurrency is checked before rate so a caller at its concurrency ceiling
        does not consume a rate token it cannot use — otherwise a saturated tenant
        drains its own rate budget doing nothing.
        """
        if self._inflight_global >= self._global_concurrency:
            raise RateLimited("global_concurrency", "global", 0.5)
        if self._inflight_workspace.get(tenant_id, 0) >= self._workspace_concurrency:
            raise RateLimited("workspace_concurrency", tenant_id, 0.5)
        domain_inflight = self._inflight_domain.get(policy.domain, 0)
        if domain_inflight >= policy.concurrent_requests:
            raise RateLimited("domain_concurrency", policy.domain, policy.effective_interval_s)

        for level, key, bucket in (
            ("domain_rate", policy.domain, self._domain_bucket(policy)),
            ("workspace_rate", tenant_id, self._workspace_bucket(tenant_id)),
            ("global_rate", "global", self._global),
        ):
            wait = bucket.take()
            if wait > 0:
                raise RateLimited(level, key, wait)

        if provider:
            bucket = self._provider.setdefault(provider, _Bucket(10.0, 10.0))
            wait = bucket.take()
            if wait > 0:
                raise RateLimited("provider_rate", provider, wait)

    def enter(self, *, tenant_id: str, domain: str) -> None:
        self._inflight_global += 1
        self._inflight_workspace[tenant_id] = self._inflight_workspace.get(tenant_id, 0) + 1
        self._inflight_domain[domain] = self._inflight_domain.get(domain, 0) + 1

    def exit(self, *, tenant_id: str, domain: str) -> None:
        """Release a slot. Clamped at zero so a double-release cannot create capacity."""
        self._inflight_global = max(0, self._inflight_global - 1)
        self._inflight_workspace[tenant_id] = max(0, self._inflight_workspace.get(tenant_id, 1) - 1)
        self._inflight_domain[domain] = max(0, self._inflight_domain.get(domain, 1) - 1)

    def snapshot(self) -> dict[str, object]:
        """Current state, for the metrics endpoint and for moving this to Redis."""
        return {
            "inflight_global": self._inflight_global,
            "inflight_by_workspace": dict(self._inflight_workspace),
            "inflight_by_domain": dict(self._inflight_domain),
            "domains_tracked": len(self._domain),
            "workspaces_tracked": len(self._workspace),
        }

