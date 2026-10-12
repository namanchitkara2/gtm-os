"""Canonicalisation, rate limits, budgets, job recovery and extraction.

The through-line: every one of these exists to stop the scraper spending money or
LinkedIn-style scarce capacity on work it has already done or should not do.
"""

from __future__ import annotations

import uuid

import pytest
from gtmos_scraping.canonical import (
    canonical_url,
    content_hash,
    registrable_domain,
    same_document,
    url_hash,
)
from gtmos_scraping.extract import (
    CompanyExtractor,
    HTMLExtractor,
    JSONLDExtractor,
    PersonExtractor,
    needs_browser,
)
from gtmos_scraping.jobs import (
    BudgetExceeded,
    CrawlBudget,
    JobStatus,
    JobType,
    ScrapeJob,
)
from gtmos_scraping.policy import (
    DomainPolicy,
    Fetcher,
    Limiter,
    RateLimited,
    RobotsPolicy,
)

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ------------------------------------------------------- canonicalisation --

class TestCanonicalisation:
    @pytest.mark.parametrize("variant", [
        "https://example.com/team",
        "https://example.com/team/",
        "https://www.example.com/team",
        "https://EXAMPLE.com/team",
        "https://example.com/team#leadership",
        "https://example.com/team?utm_source=newsletter&utm_medium=email",
        "https://example.com//team",
        "https://example.com:443/team",
        "https://example.com/team/index.html",
    ])
    def test_all_these_are_one_cache_entry(self, variant):
        """Nine fetches of one page is what an un-normalised cache buys."""
        assert canonical_url(variant) == "https://example.com/team"

    def test_meaningful_query_parameters_are_preserved(self):
        """The failure mode of over-normalising: reading page 1 forever."""
        assert canonical_url("https://x.com/jobs?page=2") == "https://x.com/jobs?page=2"
        assert "id=88" in canonical_url("https://x.com/p?id=88&utm_source=x")

    def test_parameter_order_does_not_create_a_second_entry(self):
        assert canonical_url("https://x.com/a?b=1&a=2") == canonical_url("https://x.com/a?a=2&b=1")

    def test_canonicalisation_is_idempotent(self):
        for url in ("https://www.X.com/a/?utm_source=q#f", "http://y.co", "https://z.io/a//b/"):
            once = canonical_url(url)
            assert canonical_url(once) == once

    def test_tracking_parameters_across_platforms_are_stripped(self):
        url = ("https://x.com/p?gclid=1&fbclid=2&li_fat_id=3&mc_cid=4&_hsenc=5"
               "&msclkid=6&ttclid=7&real=keep")
        assert canonical_url(url) == "https://x.com/p?real=keep"

    def test_www_is_stripped_only_as_a_leading_label(self):
        assert registrable_domain("https://wwwsomething.com/") == "wwwsomething.com"
        assert registrable_domain("https://www.example.com/") == "example.com"

    def test_same_document_ignores_fragments_and_trackers(self):
        assert same_document("https://x.com/a#top", "https://www.x.com/a/?utm_source=z")
        assert not same_document("https://x.com/a", "https://x.com/b")

    def test_content_hash_detects_change_and_ignores_identity(self):
        assert content_hash("<html>a</html>") == content_hash(b"<html>a</html>")
        assert content_hash("a") != content_hash("b")

    def test_url_hash_is_stable_and_short(self):
        assert url_hash("https://www.x.com/a/") == url_hash("https://x.com/a")
        assert len(url_hash("https://x.com")) == 32


# ------------------------------------------------------------ domain policy --

class TestDomainPolicy:
    def test_robots_disallow_is_a_refusal_not_a_slowdown(self):
        policy = DomainPolicy("x.com", disallowed_paths=("/private", "/admin"))
        allowed, why = policy.may_fetch("/private/data")
        assert allowed is False and "robots.txt" in why

    def test_allowed_paths_pass(self):
        policy = DomainPolicy("x.com", disallowed_paths=("/private",))
        assert policy.may_fetch("/about") == (True, None)

    def test_a_blocked_domain_fails_with_a_readable_reason(self):
        policy = DomainPolicy("x.com", blocked=True, blocked_reason="403 on every request")
        allowed, why = policy.may_fetch("/")
        assert allowed is False and why == "403 on every request"

    def test_tenant_owned_domain_may_ignore_robots(self):
        """Legitimate case: a customer scraping their own site."""
        policy = DomainPolicy(
            "mine.com", robots_policy=RobotsPolicy.OWNED_BY_TENANT,
            disallowed_paths=("/private",),
        )
        assert policy.may_fetch("/private")[0] is True

    def test_published_crawl_delay_beats_our_faster_default(self):
        policy = DomainPolicy("x.com", requests_per_second=10.0, crawl_delay_s=5.0)
        assert policy.effective_interval_s == 5.0

    def test_our_limit_wins_when_it_is_the_slower_one(self):
        policy = DomainPolicy("x.com", requests_per_second=0.1, crawl_delay_s=1.0)
        assert policy.effective_interval_s == 10.0


# ------------------------------------------------------------ rate limiting --

class TestRateLimiting:
    def test_domain_rate_is_shared_across_tenants(self):
        """Politeness is not ours to divide: the site sees one stream.

        If each tenant got its own domain bucket, ten tenants would hit the site at
        ten times its published rate and all of them would be blocked.
        """
        limiter = Limiter(global_rps=100, workspace_rps=100)
        policy = DomainPolicy("shared.com", requests_per_second=1.0, concurrent_requests=5)
        limiter.check(tenant_id=str(TENANT_A), policy=policy)
        with pytest.raises(RateLimited) as exc:
            limiter.check(tenant_id=str(TENANT_B), policy=policy)
        assert exc.value.level == "domain_rate"

    def test_one_tenant_cannot_exhaust_the_pool(self):
        limiter = Limiter(workspace_concurrency=2, global_concurrency=10)
        policy = DomainPolicy("x.com", concurrent_requests=10)
        for _ in range(2):
            limiter.enter(tenant_id=str(TENANT_A), domain="x.com")
        with pytest.raises(RateLimited) as exc:
            limiter.check(tenant_id=str(TENANT_A), policy=policy)
        assert exc.value.level == "workspace_concurrency"
        # The other tenant is unaffected — that is the point of the level.
        limiter.check(tenant_id=str(TENANT_B), policy=policy)

    def test_rate_limited_says_when_to_retry(self):
        """Backpressure needs a number; without it a queue spins."""
        limiter = Limiter()
        policy = DomainPolicy("x.com", requests_per_second=0.5)
        limiter.check(tenant_id=str(TENANT_A), policy=policy)
        with pytest.raises(RateLimited) as exc:
            limiter.check(tenant_id=str(TENANT_A), policy=policy)
        assert exc.value.retry_after_s > 0

    def test_releasing_a_slot_frees_capacity(self):
        limiter = Limiter(workspace_concurrency=1)
        policy = DomainPolicy("x.com", concurrent_requests=5)
        limiter.enter(tenant_id=str(TENANT_A), domain="x.com")
        with pytest.raises(RateLimited):
            limiter.check(tenant_id=str(TENANT_A), policy=policy)
        limiter.exit(tenant_id=str(TENANT_A), domain="x.com")
        limiter.check(tenant_id=str(TENANT_A), policy=policy)

    def test_double_release_cannot_manufacture_capacity(self):
        """An exception path that releases twice must not raise the ceiling."""
        limiter = Limiter(workspace_concurrency=1)
        limiter.enter(tenant_id=str(TENANT_A), domain="x.com")
        for _ in range(5):
            limiter.exit(tenant_id=str(TENANT_A), domain="x.com")
        snap = limiter.snapshot()
        assert snap["inflight_global"] == 0
        assert snap["inflight_by_workspace"][str(TENANT_A)] == 0

    def test_concurrency_is_checked_before_rate(self):
        """A saturated tenant must not drain its rate budget doing nothing."""
        limiter = Limiter(workspace_concurrency=1, workspace_rps=100)
        policy = DomainPolicy("x.com", requests_per_second=100, concurrent_requests=5)
        limiter.enter(tenant_id=str(TENANT_A), domain="x.com")
        with pytest.raises(RateLimited) as exc:
            limiter.check(tenant_id=str(TENANT_A), policy=policy)
        assert "concurrency" in exc.value.level


# ----------------------------------------------------------- crawl budgets --

class TestCrawlBudget:
    def test_page_limit_stops_an_unbounded_crawl(self):
        budget = CrawlBudget(max_pages=3, max_requests=100)
        for _ in range(3):
            budget.charge(pages=1)
        with pytest.raises(BudgetExceeded) as exc:
            budget.charge(pages=1)
        assert exc.value.limit == "max_pages"

    def test_cost_ceiling_catches_browser_spend_a_page_count_would_miss(self):
        """25 pages is cheap over HTTP and expensive in a browser."""
        budget = CrawlBudget(max_cost_usd=0.01, max_browser_seconds=10_000)
        with pytest.raises(BudgetExceeded) as exc:
            budget.charge(browser_seconds=100)
        assert exc.value.limit == "max_cost_usd"

    def test_llm_tokens_are_metered(self):
        budget = CrawlBudget(max_llm_tokens=1000, max_cost_usd=100)
        with pytest.raises(BudgetExceeded) as exc:
            budget.charge(llm_tokens=1001)
        assert exc.value.limit == "max_llm_tokens"

    def test_bytes_are_metered(self):
        budget = CrawlBudget(max_bytes=1000, max_cost_usd=100)
        with pytest.raises(BudgetExceeded):
            budget.charge(nbytes=2000)

    def test_consumption_is_recorded_even_when_the_charge_trips_the_limit(self):
        """Charge-then-check: a release missed on an exception path leaks budget."""
        budget = CrawlBudget(max_pages=1)
        budget.charge(pages=1)
        with pytest.raises(BudgetExceeded):
            budget.charge(pages=1, requests=1)
        assert budget.requests_made == 1, "the spend happened and must be recorded"

    def test_usage_report_has_every_billable_dimension(self):
        budget = CrawlBudget()
        budget.charge(pages=2, requests=3, nbytes=1000, browser_seconds=1.5, llm_tokens=100)
        usage = budget.usage()
        for key in ("pages_fetched", "requests", "bytes", "browser_seconds",
                    "llm_tokens", "provider_cost_usd", "total_cost_usd", "elapsed_s"):
            assert key in usage, key
        assert usage["total_cost_usd"] > 0


# ------------------------------------------------- jobs, retries, recovery --

class TestJobLifecycle:
    def _job(self, **kw):
        return ScrapeJob(tenant_id=TENANT_A, target_url="https://x.com/a", **kw)

    def test_a_job_is_always_owned_by_a_workspace(self):
        with pytest.raises(TypeError):
            ScrapeJob(target_url="https://x.com/")  # type: ignore[call-arg]

    def test_the_url_is_canonicalised_at_construction(self):
        job = ScrapeJob(tenant_id=TENANT_A, target_url="https://WWW.x.com/a/?utm_source=q")
        assert job.canonical == "https://x.com/a"
        assert job.domain == "x.com"

    def test_two_tenants_scraping_one_page_are_two_jobs(self):
        """Collapsing them would leak that another customer is interested."""
        a = ScrapeJob(tenant_id=TENANT_A, target_url="https://x.com/a")
        b = ScrapeJob(tenant_id=TENANT_B, target_url="https://x.com/a")
        assert a.idempotency_key != b.idempotency_key

    def test_the_same_page_requested_twice_dedupes_within_a_tenant(self):
        a = ScrapeJob(tenant_id=TENANT_A, target_url="https://x.com/a?utm_source=1")
        b = ScrapeJob(tenant_id=TENANT_A, target_url="https://www.x.com/a/")
        assert a.idempotency_key == b.idempotency_key

    def test_retry_uses_exponential_backoff(self):
        job = self._job(max_attempts=4)
        delays = []
        for _ in range(3):
            job.fail(code="PROVIDER_TIMEOUT", detail="timed out")
            delays.append(job.backoff_s())
        assert delays == sorted(delays) and delays[0] < delays[-1]

    def test_out_of_attempts_goes_to_dead_letter_not_failed(self):
        """The distinction is whether a human should look at it."""
        job = self._job(max_attempts=2)
        job.fail(code="HTTP_500", detail="server error")
        assert job.status is JobStatus.RETRY_WAIT
        job.fail(code="HTTP_500", detail="server error")
        assert job.status is JobStatus.DEAD_LETTER
        assert job.is_terminal

    def test_failure_releases_the_worker_lease(self):
        """A crashed worker's job must return to the queue, not stay locked."""
        job = self._job()
        job.locked_by = "worker-3"
        job.fail(code="CRASH", detail="worker died")
        assert job.locked_by is None and job.locked_until is None

    def test_cancellation_is_idempotent_and_honest_about_it(self):
        job = self._job()
        assert job.cancel() is True
        assert job.cancel() is False, "already cancelled; nothing changed"

    def test_a_completed_job_cannot_be_cancelled(self):
        job = self._job()
        job.status = JobStatus.COMPLETED
        assert job.cancel() is False

    def test_error_message_is_actionable_not_provider_failed(self):
        """The request's example, asserted as behaviour."""
        job = self._job(max_attempts=3, job_type=JobType.COMPANY_WEBSITE)
        job.fail(code="PROVIDER_TIMEOUT", detail="Apollo enrichment request timed out after 10s.")
        message = job.error_message()
        assert "PROVIDER_TIMEOUT" in message
        assert "Attempt: 1/3" in message
        assert str(job.id) in message
        assert "Retrying automatically" in message

    def test_log_context_always_carries_the_workspace(self):
        job = self._job(user_id=uuid.uuid4(), execution_id=uuid.uuid4())
        job.fetcher = Fetcher.HTTP
        ctx = job.log_context()
        for key in ("scrape_job_id", "workspace_id", "user_id", "execution_id",
                    "domain", "url", "fetch_strategy", "status", "retry_count"):
            assert key in ctx, key
        assert ctx["workspace_id"] == str(TENANT_A)


# ------------------------------------------------------------- extraction --

class TestExtraction:
    COMPANY_PAGE = """
    <html><head><title>About Us | Acme Inc</title>
    <meta property="og:description" content="Acme builds cloud software.">
    <link rel="canonical" href="https://acme.com/about">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization","name":"Acme Inc",
     "url":"https://acme.com","numberOfEmployees":{"@type":"QuantitativeValue","value":350},
     "address":{"@type":"PostalAddress","addressLocality":"Bangalore","addressCountry":"IN"},
     "email":"hello@acme.com"}
    </script></head>
    <body><h1>About Acme</h1><p>We are a team of 350 people in Bangalore.</p>
    <p>Reach us at hello@acme.com or noreply@acme.com</p>
    <table><tr><th>Metric</th><th>Value</th></tr><tr><td>Founded</td><td>2019</td></tr></table>
    <a href="/team">Our team</a><script>var x = "not content";</script>
    <style>.a{color:red}</style></body></html>
    """

    def test_structured_data_beats_inference_on_confidence(self):
        """The core rule: a company's own assertion outranks our guess."""
        out = CompanyExtractor().extract(self.COMPANY_PAGE, url="https://acme.com/about")
        assert out.values["company_name"] == "Acme Inc"
        assert out.evidence["company_name"].confidence >= 0.9, "schema.org, not a title guess"
        assert out.values["employee_count"] == 350
        assert out.evidence["employee_count"].confidence >= 0.9

    def test_inferred_headcount_is_marked_lower_than_asserted(self):
        page = "<html><body><p>We are a team of 420 people.</p></body></html>"
        out = CompanyExtractor().extract(page, url="https://x.com/")
        assert out.values["employee_count"] == 420
        assert out.evidence["employee_count"].confidence < 0.8
        assert "inferred" in out.evidence["employee_count"].note

    def test_junk_addresses_are_not_extracted(self):
        """A bounced send costs sender reputation; finding nothing is cheaper."""
        out = CompanyExtractor().extract(self.COMPANY_PAGE, url="https://acme.com/about")
        assert "hello@acme.com" in out.values["emails"]
        assert not any("noreply" in e for e in out.values["emails"])

    def test_script_and_style_are_not_page_text(self):
        out = HTMLExtractor().extract(self.COMPANY_PAGE, url="https://acme.com/about")
        assert "not content" not in out.values["text"]
        assert "color:red" not in out.values["text"]

    def test_tables_and_links_are_captured(self):
        out = HTMLExtractor().extract(self.COMPANY_PAGE, url="https://acme.com/about")
        assert ["Founded", "2019"] in out.values["tables"][0]
        assert "https://acme.com/team" in out.values["links"], "relative links resolved"

    def test_every_value_carries_evidence(self):
        out = CompanyExtractor().extract(self.COMPANY_PAGE, url="https://acme.com/about")
        for name in out.values:
            assert name in out.evidence, f"{name} has no evidence"
            assert out.evidence[name].source_url

    def test_evidence_payload_answers_where_did_this_come_from(self):
        out = CompanyExtractor().extract(self.COMPANY_PAGE, url="https://acme.com/about?utm_source=x")
        payload = out.to_payload(
            url="https://acme.com/about?utm_source=x", body=self.COMPANY_PAGE,
            source_type="company_website",
        )
        ev = payload["evidence"]
        assert ev["url"] == "https://acme.com/about", "tracking params stripped"
        assert ev["source_type"] == "company_website"
        assert len(ev["content_hash"]) == 64
        assert ev["extractor"] == "company_extractor"
        assert ev["extractor_version"]
        assert ev["retrieved_at"]

    def test_malformed_jsonld_does_not_fail_the_page(self):
        page = ('<html><head><title>X Corp</title>'
                '<script type="application/ld+json">{not json at all,,}</script>'
                '</head><body><p>hello</p></body></html>')
        out = CompanyExtractor().extract(page, url="https://x.com/")
        assert out.values.get("company_name"), "one bad script tag must not lose the page"

    def test_quantitative_value_and_string_headcounts_both_parse(self):
        for raw in ('"350"', '350', '{"@type":"QuantitativeValue","value":350}',
                    '"350 employees"'):
            page = (f'<html><head><script type="application/ld+json">'
                    f'{{"@type":"Organization","name":"X","numberOfEmployees":{raw}}}'
                    f'</script></head><body>x</body></html>')
            out = JSONLDExtractor().extract(page, url="https://x.com/")
            assert out.values.get("employee_count") == 350, raw

    def test_people_are_paired_with_their_roles(self):
        page = """<html><body>
        <div><h3>Jane Doe</h3><p>Chief Technology Officer</p></div>
        <div><h3>Ravi Kumar</h3><p>VP Marketing</p></div>
        </body></html>"""
        out = PersonExtractor().extract(page, url="https://x.com/team")
        names = {p["name"] for p in out.values["people"]}
        assert {"Jane Doe", "Ravi Kumar"} <= names

    def test_person_extractor_says_when_the_llm_tier_is_warranted(self):
        """Deterministic first; escalation is explicit, not a silent default."""
        out = PersonExtractor().extract("<html><body><p>nothing</p></body></html>",
                                        url="https://x.com/")
        assert not out.values.get("people")
        assert any("LLM tier" in n for n in out.notes)

    def test_deeply_nested_jsonld_does_not_hang(self):
        """Attacker-supplied JSON must not be walked unbounded."""
        nested = '{"@graph":[' * 200 + '{"@type":"Organization","name":"X"}' + ']}' * 200
        page = f'<html><head><script type="application/ld+json">{nested}</script></head></html>'
        out = JSONLDExtractor().extract(page, url="https://x.com/")
        assert isinstance(out.values, dict)


class TestFetchStrategy:
    def test_a_content_rich_page_does_not_need_a_browser(self):
        """The decision that keeps the browser pool small."""
        page = "<html><body>" + "<p>Real content about the company. </p>" * 30 + "</body></html>"
        needed, why = needs_browser(page)
        assert needed is False, why

    @pytest.mark.parametrize("page,marker", [
        ('<html><body><div id="root"></div></body></html>', "SPA"),
        ('<html><body><div id="app"></div><script>window.__NUXT__={}</script></body></html>', ""),
        ('<html><body><div data-reactroot=""></div></body></html>', ""),
        ("", "empty"),
    ])
    def test_client_rendered_shells_escalate_to_browser(self, page, marker):
        needed, why = needs_browser(page)
        assert needed is True, why

    def test_fetch_strategies_are_ordered_cheapest_first(self):
        order = list(Fetcher)
        assert order.index(Fetcher.CACHED) < order.index(Fetcher.HTTP)
        assert order.index(Fetcher.HTTP) < order.index(Fetcher.BROWSER)
