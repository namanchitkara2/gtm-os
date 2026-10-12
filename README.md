# GTM-OS

Agentic sourcing and outreach for recruiters and sales teams. Users connect from
their **own** Claude Code / Codex; the platform decomposes GTM work into
traceable graphs, enriches people with emails and phones, and keeps each user's
LinkedIn identity on their own machine.

## Status

| Phase | State |
|---|---|
| 0 — foundation (schemas, DB, bridge protocol) | **done, verified** |
| 1 — vertical slice (6 MCP tools, sourcing workflow, traces) | **running locally** |
| 1b — trace UI (**done**), OAuthProxy→Cognito + LLM structuring | in progress |
| 1c — **memory layer: provenance, decisions, signals, approvals** | **done, verified** |
| 1c — cost routing: router + one call site + ledger (**no BYOK provider clients yet**) | **done, verified** |
| 2 — recruiting pack, phones, web-scrape fallback | not started |
| 3 — autonomous sending + safety rails | not started |
| 4 — billing, org management, hardening | not started |

**Verified working end to end.** From Claude Code: `define_target` → `start_run`
→ `get_run` → `get_results` sourced three real "head of growth" profiles in
India in 18s using 4 LinkedIn actions. Re-running the same target used **1**
(cache hit). Also verified: device-code pairing, WSS attach, unknown-tool
rejection, and write dedup across redispatch.

## Run it

```bash
./run-local.sh            # postgres + control plane + bridge + dashboard
./run-local.sh status
open http://127.0.0.1:8080                                   # dashboard
claude mcp add --transport http gtm-os http://127.0.0.1:8080/mcp
```

## Architecture

Diagrams (system, a sourcing run, enrichment routing, memory layer): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Layout

```
apps/control-plane/   MCP server, 6 tools, bridge tunnel, run executor (working)
apps/graph-runner/    LangGraph worker (nodes currently run in-process)(empty)
apps/bridge/          Local agent — LinkedIn stays on the user's box   (working)
apps/web/             Dashboard: traces, people, bridge, pairing       (working)
packages/schemas/     Person, Company, Target, Run, Evidence, protocol (working)
packages/providers/   Cache-first cost-aware enrichment routing        (working)
packages/graphs/      Graph definitions + workflow packs               (empty)
db/migrations/        Postgres schema with row-level security          (working)
                      0001 state · 0002 memory (16 tables, RLS)
infra/                AWS CDK                                          (empty)
```

## The memory layer

`0002_memory.sql` is what separates this from a sourcing script. State was never
the hard part; **remembering** is. Sixteen tables, all tenant-scoped and covered
by the same RLS policy as 0001, holding the things every other tool throws away:
per-field provenance with an age, the evidence behind a claim, qualification
decisions and *why*, interactions including what someone objected to, profile
snapshots and the signals their diffs produce, an enrichment ledger, approval
policies, and outcomes per sent action.

Two constraints in that schema are the product, not bookkeeping:

- **A model cannot record a verdict with no evidence** (`model_verdicts_need_evidence`).
  A human can, because then the human is the evidence.
- **A message claim has a `NOT NULL` foreign key to evidence.** "Never fabricate
  personalisation" is a constraint, not a line in a prompt.

Nine tools read and write it: `recall`, `record_decision`, `remember`,
`whats_changed`, `rediscover`, `enrich`, `review_queue`, `decide_review`,
`spend_report`.
The one to call first is `recall` — it costs nothing, and it is the difference
between "I see you moved to Stripe" and re-sending an email the same person
ignored four months ago.

`rediscover` is the cheapest pipeline in the product: people already in memory
whose situation changed since we last judged them. Zero enrichment spend, zero
LinkedIn actions. A considered "no" stays buried until something about them
actually changes.

`enrich` is the single call site for `packages/providers`: cache within TTL, then
the cheapest source that could clear the field's confidence bar, stopping the
moment one does, with every attempt — including the ones that spent nothing —
written to `enrichment_ledger`. **No real provider clients are wired yet**, so
today it returns "not found" and a truthful trail; the routing, the ledger and
`spend_report`'s cost-per-qualified maths are live and tested.

Why this shape: see `docs/bottlenecks.md` and ADR-006..013 in
`docs/architecture-decisions.md`. The short version is that the platform can
perform roughly 100 LinkedIn actions per user per day, forever — so the cache is
the throughput, and a cache with timestamps and reasons on it is memory.

## The dashboard

`apps/web` is a Vite + React SPA served by the control plane itself, reading a
JSON API (`/api/*`) that projects the same tables the MCP tools read. Screens:
overview, runs, one run's trace, people, targets, bridge status, and the device-
code approval page.

Two things it is not. It is **not where work is started** — runs begin in the
user's own agent, and this is where they are watched. And it does **not reshape
MCP tool output into an API**: a tool's return value is a product decision about
what a model should see, and letting a table layout pull on it is how that
quietly becomes an interface nobody designed.

It is served by the control plane rather than its own origin because `pair_start`
builds its verification URL from `GTMOS_PUBLIC_URL` — this service's own origin.
A separate web host would 404 the first link a new user ever clicks. In
production the same `dist/` sits behind CloudFront on that origin.

```bash
cd apps/web && npm install && npm run dev   # :5173, proxies /api to :8080
cd apps/web && npm run build                # dist/, picked up by the control plane
```

The plan file says Next.js. Vite is used instead: the target is a static bundle
on S3+CloudFront, and there is no SSR worth having in a signed-in dashboard.

Two rough edges, both deliberate for now. The shell polls `/api/me`, `/api/bridges`
and `/api/runs` on their own timers and keeps doing so on screens that read none
of them — cheap, but more timers than any one screen needs. And SPA paths are
enumerated in `WEB_PATHS` rather than served from a catch-all, because a catch-all
placed ahead of the MCP mount would swallow the `/.well-known/...` documents
`OAuthProxy` publishes there, and that breaks discovery in a way nobody would
trace back to a routing table.

## Known gaps

- **Auth is dev-mode only.** `GTMOS_DEV_MODE=1` maps every caller to one seeded
  workspace, for the `/api/*` routes as much as for MCP — both resolve identity
  through the same `resolve_identity()`, which raises outside dev mode so a
  misconfigured deployment serves nobody. Cognito behind FastMCP's `OAuthProxy`
  is next.
- **`/bridge/pair/approve` still trusts its request body** for `tenant_id` and
  `user_id`. The dashboard does not use it — it calls `/api/bridge/pair/approve`,
  which takes only the code and resolves the principal server-side. The older
  route stays until Cognito lands and it can be deleted.
- **Structuring is heuristic, not model-backed.** `parse_profile_text` reads the
  page layout. The Haiku node needs `ANTHROPIC_API_KEY` and replaces it.
- **Scoring is keyword overlap** and says so in every rationale.
- **No enrichment yet** — emails/phones are Phase 2.
- Upstream returns fewer profile links than rendered results (7 of 10 on a real
  search). Runs report this as `seen_but_unreachable` rather than hiding it.

## Local development

```bash
uv venv && uv pip install -e packages/schemas -e apps/bridge -e apps/control-plane

# Postgres with the schema and a dev tenant
docker run -d --name gtmos-pg -e POSTGRES_PASSWORD=gtmos -e POSTGRES_USER=gtmos \
  -e POSTGRES_DB=gtmos -p 55432:5432 postgres:16-alpine
docker exec -i gtmos-pg psql -U gtmos -d gtmos < db/migrations/0001_init.sql

.venv/bin/python -m pytest tests apps/bridge/tests -q
```

## The three things that shape this codebase

1. **One LinkedIn action at a time, per user.** The upstream server's profile
   lease enforces it. Realistic ceiling is ~80–150 reads/day/user, so the cache
   is not an optimisation — it is the product's throughput.
2. **LinkedIn returns raw text, not data.** Structuring it server-side is what
   makes a database, a cache, and a dedupe key possible.
3. **A write must never happen twice.** See `docs/BRIDGE.md`.

## Attribution

`apps/bridge` depends on [`mcp-server-linkedin`](https://github.com/stickerdaniel/linkedin-mcp-server)
(Apache-2.0) by Daniel Sticker, used as a published package — see `NOTICE`.
