# Production readiness

**GTM-OS is not production-ready.** This document records what was verified, what
was built, and what is still missing — in that order, because the value of the
document is that it can be trusted.

Date: 26 September 2026. Branch: `production-hardening`.

---

## 1. Verified starting state

Every line below was confirmed by inspection or execution, not read from the
previous README. Where the README and the code disagreed, the code won.

| Claim | Verified how | Result |
|---|---|---|
| Test suite | `pytest tests apps/bridge/tests packages/providers/tests` | 76 passed |
| Test suite is larger than documented | `packages/providers/tests` sits outside the README's command | Real total was 76 across 3 paths, not 63 |
| Control plane up | `curl /api/me` | 200 |
| Bridge, Postgres, dashboard | `./run-local.sh status` | all up; Postgres up 3 days |
| Auth is dev-mode only | `app.py:57` `resolve_identity()` | Maps **every** caller to one seeded workspace |
| No enrichment providers | `ls packages/providers/src/gtmos_providers/` | `base, builtin, cache, router` only — no vendor client |
| `infra/` empty | `ls -la infra/` | 0 files |
| `packages/graphs` minimal | 1 module (`methodology.py`) | confirmed |
| `apps/graph-runner` empty | 0 source files | confirmed |
| Brief parsing broken | read `tools.py:88` | **root cause found — see §2** |
| RLS present | 28 tables with `rowsecurity = t` | present but **entirely untested** |
| `gtmos_app` role | `pg_roles.rolbypassrls` | `false` — correct |
| No git remote | `git remote -v` | empty. 3 commits + 347 untracked lines existed **only** on an external SSD |

### The parser root cause

`define_target` built its `ICP` **exclusively** from optional keyword arguments
supplied by the calling agent:

```python
icp = ICP(titles=titles or [], locations=locations or [], ...)
```

A client passing only `brief=` therefore produced an ICP with every list empty,
which serialised to `interpreted_as: {}`. Nothing rejected it. `start_run` loaded
that empty spec, the source node fell back to a stale cached `"head of growth"`
keyword, and the run spent **6 LinkedIn actions out of a ~100/day hard ceiling**
returning 7 people unrelated to the ask.

This was not a missing feature. It was a design that delegated the product's most
important transformation to the client, with no schema, no version, no tests, and
no floor.

---

## 2. What was implemented

### Phase 0 — codebase protection (done)

- Branch `production-hardening` created off `main`.
- 347 lines of untracked in-flight bridge work (`mcp_child.py`, `routing.py`,
  `web.py`, `test_routing.py`) committed rather than left at risk on removable
  media.
- `.gitignore` audited: already correct. `.env`, credentials, caches, `dist/`,
  `cdk.out/` all excluded; only `.env.example` is tracked.
- Secret scan over tracked files (`sk-`, `AKIA`, `ghp_`, PEM headers): **clean**.
- **No remote was invented.** See §5 — this is still the most urgent item.

### Phase 3 — brief parser and execution gating (done)

`packages/graphs/src/gtmos_graphs/brief.py`, wired into `tools.py`.

- Server-side deterministic parser, versioned (`PARSER_VERSION`). Titles composed
  from a seniority × function lexicon; place canonicalisation including adjectival
  forms ("Indian" → India); headcount ranges, `N+` and min/max phrasings; `Series
  B/C` compound stages; named-account extraction with the "at least 100 employees"
  false-positive guarded explicitly.
- Caller hints are merged **over** the parse per field, in both directions: a
  silent client still gets a real spec, and a client passing only `titles` does not
  lose the location the brief stated. Empty hints cannot blank a good parse — that
  was the original bug in one line.
- **Confidence gates execution.** `headcount`, `funding_stages` and `locations`
  are deliberately **not** discriminating criteria: LinkedIn people-search cannot
  filter on the first two, so a spec carrying only those would pass a naive
  emptiness check and search the entire platform.
- **Two independent gates.** `define_target` refuses and does not persist.
  `start_run` refuses again at the point the budget is actually spent, because
  targets are reusable objects that outlive the call that created them and rows
  written before the parser exists carry empty specs.
- Refusals are machine-actionable (`error`, `why`, `needed`, `how_to_fix`,
  `parsed_so_far`), because "could not parse brief" sends a client into a retry
  loop with the same words.
- 33 tests including the named Ourshopee regression.

### Phase 1 (partial) — tenant isolation proven, auth hole closed

`tests/test_tenant_isolation.py` — 15 tests against **live Postgres**:

- Tenant A cannot select, count, update, delete, or insert into tenant B's rows,
  including when it passes B's UUIDs directly (IDOR) and when it names B's
  `tenant_id` in a `WHERE` clause.
- Tenant A cannot reassign its own row to B (exfiltration by ownership transfer).
- **Fail-closed verified:** an unscoped connection returns zero rows, not every
  row. This is the property that makes a forgotten `db.scoped()` surface as
  missing data rather than as a breach.
- **No scope leak across pooled connections** — the test that would catch someone
  "fixing" a missing-rows bug by changing `SET LOCAL` to `SET`.
- The app role cannot bypass or disable RLS.
- All 22 tenant-owned tables verified to have RLS **on** and a policy that
  actually references `app.tenant_id` — RLS enabled with no policy denies
  everything; a policy that ignores the scope protects nothing.

`apps/control-plane/tests/test_auth_gates.py` — 7 tests:

- `resolve_identity()` fails closed outside dev mode, and is gated on
  `GTMOS_DEV_MODE == "1"` exactly, not on truthiness (`"false"` and `"0"` are both
  truthy strings).
- `resolve_identity()` takes **no parameters** — the strongest form of "a caller
  cannot name its own tenant".
- Legacy `POST /bridge/pair/approve`, which trusted `tenant_id` from its request
  body, now **404s outside dev mode**. Deployed, that route was an
  account-takeover primitive: a bridge is a live LinkedIn session, and attaching
  one to a workspace you merely named was all it took. Gated rather than deleted
  because `scripts/dev_pair.py` and `scripts/e2e.py` depend on it; local pairing
  was re-verified working.

### Web scraping subsystem (security core done, execution layer not)

New package `packages/scraping`, 155 tests. Deliberately the **only** path to the
network, because SSRF containment and injection containment are properties of the
*system* only if no workflow can call `httpx` directly.

| Module | What it does |
|---|---|
| `ssrf.py` | The gate. Cloud metadata endpoints (AWS/GCP/Azure/Alibaba/Oracle), private/loopback/link-local/reserved ranges, IPv4-mapped and 6to4 and Teredo smuggling, embedded credentials, CRLF, control characters, service ports, per-hop redirect revalidation, oversized responses, decompression bombs. **Resolves DNS once and returns the addresses** so the fetcher can pin them — re-resolving at connect time is the DNS-rebinding window. Every resolved address is checked, not just the first. |
| `untrusted.py` | Scraped content is data. Nonced delimiters (a static `<content>` tag is forgeable by a page that writes the closing tag), schema-constrained output validation, obfuscation-aware detection covering zero-width characters, Unicode tag channels, bidi overrides, HTML comments and CSS-hidden text. Detection **flags and caps confidence at 0.3; it never sanitises**, because cleaning the matched phrase produces content that looks safe and is still attacker-shaped everywhere else. |
| `canonical.py` | URL canonicalisation as a **deny-list** of ~35 tracking parameters, never an allow-list — dropping unknown parameters silently breaks `?page=2` and makes a crawler read page 1 forever while reporting a healthy cache hit rate. Idempotent. Content hashing for change detection. |
| `policy.py` | Per-domain politeness **shared across tenants** (the site sees one stream; per-tenant domain buckets would hit it at N× its published rate), per-workspace fairness, published `crawl-delay` wins when slower than our default, token buckets with continuous refill. Robots disallow is a refusal with a reason — there is no bypass anywhere in this package. |
| `jobs.py` | `ScrapeJob` with `tenant_id` **required and defaulted nowhere**, canonicalised at construction, tenant-scoped idempotency key. `CrawlBudget` bounds pages/depth/bytes/duration/requests/browser-seconds/LLM-tokens with a **cost backstop** so an expensive browser page cannot slip past a page count. Worker leases, exponential backoff, dead-letter distinct from failed, and actionable error text matching the format requested. |
| `extract.py` | JSON-LD before inference, with different confidences — a company asserting its headcount in schema.org (0.9) is not the same claim as us regexing it out of prose (0.6). Every value carries `Evidence` from the **existing** `gtmos_schemas` rather than a second provenance system. `needs_browser()` keeps the browser pool small by escalating only on a real content shortfall. |

Two bugs the tests found and fixed: `CrawlBudget.check` used `used >= cap`, so
`max_pages=3` fetched two pages; and the domain token bucket was sized to the
concurrency limit, letting five tenants burst straight past a shared 1 rps limit.

---

## 2a. Web scraping subsystem report

Reported against the 14 items requested. **The subsystem is not production-ready:
it is a tested security core with no execution layer.**

| Item | Status | Detail |
|---|---|---|
| Architecture implemented | **PARTIAL** | The layers that decide *whether and how* to fetch exist and are tested. The layers that actually fetch do not. |
| Supported fetch strategies | **PARTIAL** | `Fetcher` enum (CACHED → HTTP → BROWSER → SEARCH) with the cheapest-first ordering asserted, and `needs_browser()` making the escalation decision on real markers (empty SPA mounts, Next/Nuxt/Angular/React roots, text shortfall). **No strategy is implemented** — nothing issues an HTTP request. |
| Queue / workers | **NOT DONE** | `ScrapeJob` has the durable shape (worker lease with expiry, attempts, exponential backoff, `RETRY_WAIT` vs `DEAD_LETTER`, cancellation, tenant-scoped idempotency key) and it is unit-tested. There is **no queue table, no migration, and no worker loop.** Nothing survives a restart because nothing is persisted. |
| Browser infrastructure | **NOT DONE** | No Playwright, no pool, no contexts, no recycling, no memory caps. `needs_browser()` only decides that one *would* be needed. |
| Extraction system | **DONE (deterministic tier)** | `HTMLExtractor`, `JSONLDExtractor`, `CompanyExtractor`, `PersonExtractor` behind a registry, single-pass parser, stdlib-only (no lxml, deliberately — it widens the XML attack surface). JSON-LD outranks text inference in confidence. LLM tier: interface and validation exist (`validate_extraction`), **no model is called.** |
| Evidence integration | **DONE** | Every extracted value carries `gtmos_schemas.Evidence` — the platform's existing provenance type, not a second system. `to_payload()` emits the requested envelope: canonical url, source_type, retrieved_at, content_hash, extractor, extractor_version, per-field confidence, plus `hostile_source`. |
| Caching | **PARTIAL** | The *keys* are done and tested: canonicalisation is idempotent, tracking-param deny-list, `content_hash` for change detection, `FRESHNESS_BY_KIND` config (job posting 6h → press release 90d). **No cache store exists** — no table, no read-through, so nothing is actually cached yet. |
| Rate limiting | **DONE (in-process)** | Global → workspace → provider → domain, all checked, all must pass. Domain politeness shared across tenants; published `crawl-delay` wins when slower. Concurrency checked before rate so a saturated tenant cannot drain its own rate budget. **Single-process only** — `Limiter.snapshot()` exists as the seam for moving state to Redis/Postgres, which multi-worker deployment requires. |
| SSRF protection | **DONE** | 60 tests. Metadata endpoints across 5 clouds, private/loopback/link-local/reserved/multicast, IPv4-mapped + 6to4 + Teredo smuggling, decimal IP notation, embedded credentials, CRLF, control chars, service ports, `.internal`/`.local` suffixes, per-hop redirect revalidation, response size, decompression bombs. Resolves DNS **once** and returns addresses for pinning; **all** resolved addresses checked, not just the first. |
| Prompt-injection protection | **DONE** | 31 tests including the named regression. Three independent mechanisms: nonced delimiters (structural), schema-constrained output (the reason a *successful* injection is still mostly harmless), and obfuscation-aware detection that flags and caps confidence at 0.3 rather than sanitising. Explicit trust ordering with `scraped_content` last. |
| Observability | **PARTIAL** | `ScrapeJob.log_context()` emits every field the request lists; `Limiter.snapshot()` exposes queue/inflight state; `CrawlBudget.usage()` emits the cost dimensions. **No logger is wired, no metrics backend, no dashboards.** |
| Billing / usage | **PARTIAL** | `CrawlBudget` meters requests, browser-seconds, bytes, LLM tokens and provider cost with a cost backstop, and `usage()` returns the row billing would consume. **Nothing consumes it** — there is no billing system (see DoD). |
| Tests | **155, all passing** | SSRF 60, pipeline 64, injection 31. Absent: browser-worker tests, real-HTTP fetch tests, redirect-following tests against a live server, queue-recovery and worker-crash tests. Those need the subsystems above to exist first. |
| Remaining external dependencies | — | Playwright (browser tier), an LLM key (extraction tier — `ANTHROPIC_API_KEY`), a queue backend (Postgres table is sufficient; none written), Redis if the limiter goes multi-process. A search-discovery provider for the SEARCH strategy. |

### The honest headline for this subsystem

**Nothing calls it.** `grep -rn "gtmos_scraping" apps/ packages/ | grep -v packages/scraping`
returns zero hits. It is a well-tested island, not yet the shared infrastructure
§22 of the request asks for. The security core is genuinely done and genuinely
valuable — it is the part that is hardest to retrofit and the part where a mistake
is unrecoverable — but no sourcing, enrichment or research path routes through it
yet, and it has never fetched a real URL.

---

## 3. Test results

```
pytest          # bare invocation; testpaths now includes tests/
301 passed in 7.5s
```

Up from 76. Note `testpaths` in the root `pyproject.toml` was `["packages", "apps"]`,
so a bare `pytest` — what CI runs — silently skipped `tests/` entirely, where the
two highest-value suites live. Fixed. Breakdown of what is new:

| Suite | Tests | Needs |
|---|---|---|
| `packages/scraping/tests/test_ssrf.py` | 60 | nothing |
| `packages/scraping/tests/test_pipeline.py` | 64 | nothing |
| `packages/scraping/tests/test_injection.py` | 31 | nothing |
| `tests/test_brief_parser.py` | 33 | nothing |
| `tests/test_tenant_isolation.py` | 15 | **live Postgres** (skips loudly if absent) |
| `tests/test_define_target_integration.py` | 11 | **live Postgres** — asserts a refused brief is not persisted and `start_run` never reaches the executor with an empty spec |
| `apps/control-plane/tests/test_auth_gates.py` | 7 | nothing |

Lint (`ruff`): new files clean apart from 3 `E501` long lines. Pre-existing code
has 37 findings, untouched — fixing them would bury the security diff in noise.

---

## 4. Definition of done

| Item | Status | Note |
|---|---|---|
| Real authentication | **BLOCKED** | Needs a Cognito user pool / Auth0 tenant. Not stubbed: a JWT validator that cannot be tested against a real issuer is worse than the honest gap, because it converts a fail-closed system into a maybe-fail-open one. What *was* done: fail-closed verified, the body-trusting approve route gated. |
| Multi-tenant isolation | **PARTIAL** | RLS layer **proven** by 15 negative tests. Application layer still resolves every caller to one workspace, so isolation is real in the database and moot above it until auth lands. |
| RBAC | **PARTIAL** | `tenant_members.role` already exists with owner/admin/member. No `viewer`, and **no permission checks are enforced anywhere in the code**. |
| Provider integrations | **BLOCKED** | Apollo's entire REST API is refused on the current plan; Prospeo needs a key. No fake responses were written. |
| Working enrichment | **BLOCKED** | Follows the above. Router, cache, ledger and cost maths are live and tested; there is nothing to route to. |
| Reliable brief parser | **PASS** | 33 tests, versioned, deterministic, gated. |
| Durable workflow engine | **NOT DONE** | Nodes still run in-process. `ScrapeJob` has the durable shape (leases, backoff, dead-letter) but no queue table or worker loop. |
| LinkedIn/action safeguards | **PARTIAL** | Pre-existing: one action at a time, daily budget, idempotent writes, ack-without-result handled. New: the parser gate, which is what stops a meaningless run spending the budget at all. |
| Billing | **BLOCKED** | Needs a Stripe account. `CrawlBudget.usage()` emits the metered rows billing would consume. |
| Organization management | **NOT DONE** | Schema supports it (`tenants`, `users`, `tenant_members`); no invitations, no member management, no ownership transfer. |
| Infrastructure as code | **NOT DONE** | `infra/` still empty. An empty directory is honest; a tree of empty CDK stacks is not. |
| Staging / production deploy path | **NOT DONE** | No deploy target exists. |
| CI/CD | **NOT DONE** | No workflow file. |
| Structured observability | **PARTIAL** | `ScrapeJob.log_context()` and `Limiter.snapshot()` emit the specified fields. No metrics backend, no `/live` `/ready` `/health` triad (only `/healthz`). |
| Security hardening | **PARTIAL** | SSRF, prompt injection, tenant isolation, the approve-route hole: done and tested. Not done: dependency CVE scan, rate limiting on the HTTP API, CSRF. |
| Data privacy controls | **PARTIAL** | Tenant isolation proven; evidence/audit trail real. No retention policy, no deletion workflow, no export, no encryption-at-rest config. |
| Comprehensive tests | **PARTIAL** | 290, and the security-critical paths are genuinely covered. No browser-worker, queue-recovery, or end-to-end signup tests — those need the subsystems above to exist. |
| Frontend on real backend state | **PARTIAL** | Pre-existing dashboard reads real data. No login, org, billing, usage or scraping screens. |
| Documentation | **PARTIAL** | This file. `ARCHITECTURE.md`, `SECURITY.md`, `DEPLOYMENT.md`, `API.md` etc. not written — documenting a deployment that does not exist is the failure mode this document is trying to avoid. |
| Web scraping subsystem | **PARTIAL** | Security core done and tested (SSRF, injection, policy, budgets, extraction, evidence). No queue, no workers, no browser pool, no cache store, and **nothing calls it**. Full report in §2a. |
| Backup / recovery | **NOT DONE** | And see §5: there is no remote at all. |
| No critical/high security issues | **PARTIAL** | The three found were fixed. No independent audit, no dependency scan. |
| No known critical blockers | **NO** | Auth, providers, infra, billing. |

**Score: 1 PASS, 10 PARTIAL, 5 BLOCKED on external credentials, 7 NOT DONE.**

---

## 5. Most urgent item, and it is not on the list above

`~/gtm-os` is a symlink to `/Volumes/Extended Hard Drive Naman Chitkara /Projects/gtm-os`
and **`git remote -v` is empty**. The entire codebase exists on one removable
drive. Every other project in this account lives on GitHub; this one does not.

```bash
gh repo create gtm-os --private --source=. --remote=origin
git push -u origin main production-hardening
```

Nothing else in this document matters if the drive fails.

---

## 6. Running it

```bash
# Local, everything
./run-local.sh                  # postgres + control plane + bridge + dashboard
./run-local.sh status
open http://127.0.0.1:8080

# Tests — all four paths. The README's command misses packages/.
.venv/bin/python -m pytest tests apps/bridge/tests apps/control-plane/tests packages -q

# Tenant isolation needs Postgres; it skips loudly rather than passing silently.
docker start gtmos-pg

# Lint
uvx ruff check packages apps tests
```

Staging and production deployment: **no path exists yet.** Not documented here,
because a documented deployment that has never run is how an outage gets its head
start.
