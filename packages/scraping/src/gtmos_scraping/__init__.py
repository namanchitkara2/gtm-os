"""Shared scraping infrastructure for GTM-OS.

One subsystem, used by sourcing, enrichment, company research and any future
agent — not a scraper per workflow. The reason is not tidiness: the security
properties here (SSRF containment, prompt-injection containment, per-domain
politeness, crawl budgets) are only properties of the *system* if there is no
second path to the network. A workflow that calls httpx directly has none of
them, so the value of this package is precisely that it is the only door.

Layering, cheapest first:

    canonical   URL normalisation and content hashing — the cache key
    ssrf        the gate: what may be fetched at all
    policy      per-domain politeness and multi-level rate limits
    jobs        the unit of work, its budget and its state machine
    extract     bytes -> claims, each carrying its evidence
    untrusted   scraped content is data, never instructions
"""

from __future__ import annotations

from .canonical import canonical_url, content_hash, registrable_domain, url_hash
from .extract import (
    EXTRACTOR_VERSION,
    EXTRACTORS,
    CompanyExtractor,
    Extracted,
    HTMLExtractor,
    JSONLDExtractor,
    PersonExtractor,
    needs_browser,
)
from .jobs import (
    TERMINAL_STATES,
    BudgetExceeded,
    CrawlBudget,
    JobStatus,
    JobType,
    ScrapeJob,
)
from .policy import (
    FRESHNESS_BY_KIND,
    DomainPolicy,
    Fetcher,
    Limiter,
    RateLimited,
    RobotsPolicy,
)
from .ssrf import (
    ALLOWED_SCHEMES,
    NetworkPolicy,
    UrlRejected,
    ValidatedTarget,
    check_response_size,
    resolve_and_validate,
    validate_redirect,
    validate_url,
)
from .untrusted import (
    TRUST_TIERS,
    ExtractionRejected,
    InjectionScan,
    scan_for_injection,
    validate_extraction,
    wrap_untrusted,
)

__all__ = [
    "ALLOWED_SCHEMES", "EXTRACTORS", "EXTRACTOR_VERSION", "FRESHNESS_BY_KIND",
    "TERMINAL_STATES", "TRUST_TIERS", "BudgetExceeded", "CompanyExtractor",
    "CrawlBudget", "DomainPolicy", "ExtractionRejected", "Extracted", "Fetcher",
    "HTMLExtractor", "InjectionScan", "JSONLDExtractor", "JobStatus", "JobType",
    "Limiter", "NetworkPolicy", "PersonExtractor", "RateLimited", "RobotsPolicy",
    "ScrapeJob", "UrlRejected", "ValidatedTarget", "canonical_url",
    "check_response_size", "content_hash", "needs_browser", "registrable_domain",
    "resolve_and_validate", "scan_for_injection", "url_hash", "validate_extraction",
    "validate_redirect", "validate_url", "wrap_untrusted",
]
