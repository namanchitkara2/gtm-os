"""The scrape job, its budget, and the state machine it moves through.

A crawl without explicit limits is an unbounded liability: one URL on a site with
pagination is an infinite crawl, and the bill arrives as bandwidth, browser
seconds and LLM tokens. So every limit in :class:`CrawlBudget` has a default, and
:meth:`CrawlBudget.charge` is the only way to spend — a caller cannot fetch without
going through the thing that says no.

The status set is the one the dashboard renders, and it includes ``EXTRACTING``
separately from ``RUNNING`` because they fail differently and for different
reasons. A job that fetched everything and failed to extract is a bug in our
extractor; a job that failed to fetch is a fact about the internet. Collapsing
them into "failed" loses the distinction that decides who is on the hook.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .canonical import canonical_url, registrable_domain
from .policy import Fetcher


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Retryable failure, waiting for its next attempt. Distinct from QUEUED so a
    #: dashboard can show "retrying in 40s" rather than pretending it never ran.
    RETRY_WAIT = "retry_wait"
    #: Out of attempts. Kept rather than deleted — a dead-letter row is the only
    #: record of a systematic failure, and deleting it hides a broken extractor.
    DEAD_LETTER = "dead_letter"


#: States from which no further work happens.
TERMINAL_STATES = frozenset({
    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.DEAD_LETTER,
})


class JobType(StrEnum):
    COMPANY_WEBSITE = "company_website"
    TEAM_PAGE = "team_page"
    CAREERS_PAGE = "careers_page"
    JOB_POSTING = "job_posting"
    PRESS_RELEASE = "press_release"
    PRICING_PAGE = "pricing_page"
    GENERIC_PAGE = "generic_page"
    SEARCH_DISCOVERY = "search_discovery"


class BudgetExceeded(Exception):
    """A limit was hit. Names which one, because the fix differs per limit."""

    def __init__(self, limit: str, used: float, cap: float) -> None:
        self.limit = limit
        self.used = used
        self.cap = cap
        super().__init__(f"{limit} budget exhausted: {used} of {cap}")


@dataclass
class CrawlBudget:
    """Hard ceilings on one job. Every field has a default, because the failure mode
    of a missing limit is unbounded spend rather than an error anyone notices.

    ``max_cost_usd`` is the backstop. The others bound a specific resource; this one
    bounds the thing the customer is actually billed for, and it is checked against
    the running total so an expensive browser page cannot slip past a page count.
    """

    max_pages: int = 25
    max_depth: int = 2
    max_bytes: int = 20 * 1024 * 1024
    max_duration_s: float = 300.0
    max_requests: int = 50
    max_cost_usd: float = 1.00
    max_browser_seconds: float = 60.0
    max_llm_tokens: int = 50_000

    pages_fetched: int = 0
    requests_made: int = 0
    bytes_downloaded: int = 0
    browser_seconds: float = 0.0
    llm_tokens: int = 0
    provider_cost_usd: float = 0.0
    started_at: datetime = field(default_factory=_utcnow)

    #: Indicative unit costs, used so a job can refuse *before* spending rather
    #: than reporting an overrun afterwards. Real provider charges replace the
    #: estimate in ``provider_cost_usd`` when a bill is known.
    COST_PER_REQUEST_USD = 0.0002
    COST_PER_BROWSER_SECOND_USD = 0.0015
    COST_PER_1K_LLM_TOKENS_USD = 0.003

    @property
    def elapsed_s(self) -> float:
        return (_utcnow() - self.started_at).total_seconds()

    @property
    def total_cost_usd(self) -> float:
        """What this job has cost so far, across every metered resource."""
        return round(
            self.provider_cost_usd
            + self.requests_made * self.COST_PER_REQUEST_USD
            + self.browser_seconds * self.COST_PER_BROWSER_SECOND_USD
            + (self.llm_tokens / 1000) * self.COST_PER_1K_LLM_TOKENS_USD,
            6,
        )

    def check(self) -> None:
        """Raise if any ceiling has been *exceeded*.

        Strictly greater-than, not greater-or-equal. With charge-then-check
        semantics, spending the last unit of a budget leaves ``used == cap``, and
        treating that as an overrun makes ``max_pages=3`` fetch two pages. The
        first version of this did exactly that and the test caught it.
        """
        for limit, used, cap in (
            ("max_pages", self.pages_fetched, self.max_pages),
            ("max_requests", self.requests_made, self.max_requests),
            ("max_bytes", self.bytes_downloaded, self.max_bytes),
            ("max_browser_seconds", self.browser_seconds, self.max_browser_seconds),
            ("max_llm_tokens", self.llm_tokens, self.max_llm_tokens),
            ("max_duration_s", self.elapsed_s, self.max_duration_s),
            ("max_cost_usd", self.total_cost_usd, self.max_cost_usd),
        ):
            if used > cap:
                raise BudgetExceeded(limit, used, cap)

    def charge(
        self,
        *,
        pages: int = 0,
        requests: int = 0,
        nbytes: int = 0,
        browser_seconds: float = 0.0,
        llm_tokens: int = 0,
        provider_cost_usd: float = 0.0,
    ) -> None:
        """Record consumption, then re-check.

        Charging *before* the check is deliberate: the alternative reserves capacity
        and then has to release it on failure, and a release that is missed on an
        exception path silently leaks budget. Overshooting the last unit is
        acceptable; leaking the ceiling is not.
        """
        self.pages_fetched += pages
        self.requests_made += requests
        self.bytes_downloaded += nbytes
        self.browser_seconds += browser_seconds
        self.llm_tokens += llm_tokens
        self.provider_cost_usd += provider_cost_usd
        self.check()

    def usage(self) -> dict[str, Any]:
        """The row that reaches the usage/billing tables and the dashboard."""
        return {
            "pages_fetched": self.pages_fetched,
            "requests": self.requests_made,
            "bytes": self.bytes_downloaded,
            "browser_seconds": round(self.browser_seconds, 3),
            "llm_tokens": self.llm_tokens,
            "provider_cost_usd": round(self.provider_cost_usd, 6),
            "total_cost_usd": self.total_cost_usd,
            "elapsed_s": round(self.elapsed_s, 3),
        }


@dataclass
class ScrapeJob:
    """One unit of scraping work, owned by exactly one workspace.

    ``tenant_id`` is not optional and has no default. Every row in this subsystem
    is tenant-owned, and a job that can be constructed without an owner is a job
    that will eventually be written without one.
    """

    tenant_id: uuid.UUID
    target_url: str
    job_type: JobType = JobType.GENERIC_PAGE
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: uuid.UUID | None = None
    execution_id: uuid.UUID | None = None
    priority: int = 100
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    depth: int = 0
    fetcher: Fetcher | None = None
    budget: CrawlBudget = field(default_factory=CrawlBudget)
    scheduled_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: Held by one worker at a time. A crashed worker's lease expires and the job
    #: returns to the queue; without the lease, a restart double-fetches everything
    #: in flight.
    locked_by: str | None = None
    locked_until: datetime | None = None
    last_error: str | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        # Canonicalising at construction rather than at fetch time is what makes
        # the dedupe key stable: two callers asking for the same page with
        # different tracking parameters must produce one job, not two.
        self.canonical = canonical_url(self.target_url)
        self.domain = registrable_domain(self.target_url)

    @property
    def idempotency_key(self) -> str:
        """What makes a re-enqueued job the same job.

        Scoped to the tenant so two workspaces scraping the same public page each
        get their own job and their own evidence — collapsing them would leak the
        fact that another customer is interested in that company.
        """
        return f"{self.tenant_id}:{self.canonical}:{self.job_type}"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def may_retry(self) -> bool:
        return self.attempts < self.max_attempts and not self.is_terminal

    def backoff_s(self, *, base: float = 2.0, cap: float = 300.0) -> float:
        """Exponential backoff for the next attempt.

        Deterministic, with jitter supplied by the caller if it wants it. A fixed
        retry interval synchronises every worker's retries into a burst against a
        site that is already failing, which is how a transient error becomes a
        block.
        """
        return min(cap, base ** max(1, self.attempts))

    def next_retry_at(self) -> datetime:
        return _utcnow() + timedelta(seconds=self.backoff_s())

    def fail(self, *, code: str, detail: str) -> JobStatus:
        """Record a failure and move to the right state.

        Returns the new status so the caller cannot forget to persist it. A job out
        of attempts goes to ``DEAD_LETTER`` rather than ``FAILED``: the distinction
        is whether a human should look at it.
        """
        self.attempts += 1
        self.last_error_code = code
        self.last_error = detail[:2000]
        self.locked_by = None
        self.locked_until = None
        if self.may_retry:
            self.status = JobStatus.RETRY_WAIT
            self.scheduled_at = self.next_retry_at()
        else:
            self.status = JobStatus.DEAD_LETTER
            self.completed_at = _utcnow()
        return self.status

    def cancel(self) -> bool:
        """Cancel unless already finished. Returns whether anything changed."""
        if self.is_terminal:
            return False
        self.status = JobStatus.CANCELLED
        self.completed_at = _utcnow()
        self.locked_by = None
        self.locked_until = None
        return True

    def error_message(self) -> str:
        """An operator-readable failure line.

        Written out because "Provider failed" is what this replaces: a message
        without the code, the attempt count and the job id cannot be acted on
        without a log search, and the log search is the expensive part.
        """
        return (
            f"{self.last_error_code or 'UNKNOWN_ERROR'}\n"
            f"{self.last_error or 'no detail recorded'}\n"
            f"Attempt: {self.attempts}/{self.max_attempts}\n"
            f"Job: {self.id}\n"
            f"URL: {self.canonical}\n"
            f"Domain: {self.domain}\n"
            f"Status: {self.status}"
            + (
                f"\nRetrying automatically at {self.scheduled_at.isoformat()}"
                if self.status is JobStatus.RETRY_WAIT
                else "\nNo further attempts; moved to dead-letter for review."
                if self.status is JobStatus.DEAD_LETTER
                else ""
            )
        )

    def log_context(self) -> dict[str, Any]:
        """Structured fields for every log line about this job.

        The list is the one the request specifies, and it is produced here rather
        than assembled per call site so no log line can be missing the tenant.
        """
        return {
            "scrape_job_id": str(self.id),
            "workspace_id": str(self.tenant_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "execution_id": str(self.execution_id) if self.execution_id else None,
            "domain": self.domain,
            "url": self.canonical,
            "fetch_strategy": str(self.fetcher) if self.fetcher else None,
            "worker_id": self.locked_by,
            "status": str(self.status),
            "retry_count": self.attempts,
            "job_type": str(self.job_type),
            "depth": self.depth,
        }
