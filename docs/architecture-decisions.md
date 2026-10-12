# Architecture decisions

ADRs for the decisions taken on 2026-09-18 as research turned into schema. Format: context,
decision, consequence, and what would reverse it. Earlier decisions (bridge-as-client, Vite over
Next, no fork of the LinkedIn MCP server) live in `BRIDGE.md` and the README and are unchanged.

---

## ADR-006 — GTM memory and the intelligence graph are one object

**Context.** The brief asked for a persistent memory layer *and* an intelligence graph connecting
person ↔ company ↔ role ↔ technology ↔ event ↔ interaction ↔ campaign ↔ opportunity. Built
separately, they would disagree within a quarter, and the graph would become a denormalised copy
whose refresh nobody owns.

**Decision.** One store. `people` and `companies` remain the nodes; `entity_edges` holds typed,
directional, tenant-scoped relationships with their own provenance; everything else
(`interactions`, `signals`, `qualification_decisions`, `research_artifacts`, `entity_snapshots`)
hangs off those nodes. Postgres, not a graph database: our queries are one and two hops with hard
tenant filters, which is exactly what an index on `(tenant_id, src_id, edge_type)` serves.

**Consequence.** No new infrastructure. Deep traversal (>3 hops) will be awkward; we do not have
a use case for it and should not acquire one casually.

**Reversed by.** A real product need for multi-hop path queries at scale — then extract into a
graph engine behind the same repository interface.

---

## ADR-007 — Provenance is a schema constraint, not a prompt instruction

**Context.** "Never fabricate personalisation" cannot be enforced by asking a model nicely, and
the EU AI Act (from 2 Aug 2026) requires explanations for significant automated decisions in
recruitment.

**Decision.** Every asserted fact carries `source`, `observed_at`, `confidence` and, where it came
from a run, `run_id`. Drafted messages store `claims[]`, each referencing an `evidence_id`. **A
claim with no evidence id fails to write.** Qualification decisions store the evidence ids they
rested on.

**Consequence.** More columns, more write-path work, and some drafts will refuse to generate.
That refusal is the feature. It also makes "why did we qualify this?" and "why did we say this
about this person?" the same query.

**Reversed by.** Nothing we can foresee. This is the load-bearing decision of the product.

---

## ADR-008 — The cache is the product, so the router is in the foundation

**Context.** The platform can perform roughly 100 LinkedIn actions per user per day, forever
(profile lease, upstream). Separately, enrichment providers charge 5–10x different prices for the
same field, and credit burn is the top user complaint about the category leader.

**Decision.** `packages/providers` ships in the P0 migration cycle, not later. Order of resolution
for any field: **local cache within TTL → cheapest provider meeting the confidence target → stop →
escalate only if confidence is still short.** Every attempt, hit or miss, is written to
`enrichment_ledger` with provider, cost, latency, confidence and outcome.

**Consequence.** Every enrichment call site must go through the router; direct provider calls
become a lint-able mistake. Freshness TTLs become product configuration, per field, per tenant.
There is exactly one call site — `apps/control-plane/.../enrichment.py` — because "every call
site goes through the router" is only enforceable when there is one.

**Amended 2026-09-18, from running it.** Eligibility is decided on a provider's *ceiling*
confidence, not its typical value. The first version filtered on the average, and with the
seeded email policy at 0.80 that excluded the cheap tier from every email lookup — Apollo's
blended confidence is 0.75 because it is ~88% in the US and 60–73% elsewhere — so the most
common field in the product routed straight to a source costing four times as much. A provider
is now dropped only when it could never clear the bar; when a cheap answer comes back short,
the waterfall escalates, which is what the waterfall is for.

**Reversed by.** Becoming a data reseller, which would invert the incentive. We should not.

---

## ADR-009 — Change detection is a budgeted queue, never a crawler

**Context.** Snapshots and change detection naturally suggest polling. At ~100 actions/day/user, a
1,000-person watchlist polled monthly would consume a third of the budget and starve active
sourcing.

**Decision.** Snapshot on every read we were already making for another reason. Diff on the next
legitimate read. Re-read only when a **cheap external signal** (job posting, funding item, site
change — none of which cost a LinkedIn action) makes it worth one. Candidates for re-read go into
a ranked, budgeted queue that the tenant's remaining daily budget drains.

**Consequence.** Change detection is eventually-consistent and unevenly distributed — high-signal
entities are fresh, quiet ones are not. That is the correct allocation of a scarce resource, and
it must be explained in the UI rather than hidden.

**Reversed by.** A legal, cheap bulk source of profile changes. It would not change the
architecture, only the queue's input.

---

## ADR-010 — Approval is a state on work, not a UI feature

**Context.** Human review currently happens outside every product, in a spreadsheet. Regulation
now requires reviewers with genuine override authority for recruitment decisions.

**Decision.** `approval_policies` (tenant × action_class → auto | require_approval | forbid) and
`approvals` (actor, verdict, reason, evidence). Run steps may enter `awaiting_approval` and
resume. The policy decides; no code path is hardcoded to ask or not ask. Default policy: outbound
messages and connection requests require approval; enrichment and reads do not.

**Consequence.** The executor needs a durable pause. We already have one — `awaiting_bridge` —
and this is the same shape.

**Reversed by.** Nothing. The default may loosen per tenant; the mechanism stays.

---

## ADR-011 — Outcomes feed the budget

**Context.** LinkedIn suspends InMail below a 13% reply rate over 100+ sends in 14 days; mailbox
providers reject outright above a 0.3% complaint rate. Both are account-ending events for a user.

**Decision.** `action_outcomes` records what happened to each sent action (replied, bounced,
complained, ignored, connection accepted). A rolling window per channel feeds the budget, which
**refuses** further sends below a configured floor and returns the reason to the calling agent.

**Consequence.** The product will sometimes tell a paying customer it will not send their message.
That is the position we are taking against a category with 50–70% churn.

**Reversed by.** Nothing short of a business-model change we should not make.

---

## ADR-012 — The copilot is a tool that returns a plan, not a chat UI

**Context.** The brief asks for natural-language workflow creation and hints at a visual builder.
Our users arrive through their own Claude Code or Codex; they already have a chat surface.

**Decision.** Expose `plan_workflow(intent) -> Plan` — a typed, inspectable plan with estimated
action count and provider cost. The user's own agent renders it, the human approves it,
`start_run` executes it. We build no editor and no chat.

**Consequence.** The plan object becomes a public contract and needs to be stable and explainable.
We give up control over presentation, which we did not want.

**Reversed by.** A non-technical buyer segment becoming the ICP.

---

## ADR-013 — New tables follow 0001's RLS pattern exactly

**Context.** `0001_init.sql` enables and forces RLS on every tenant-scoped table with a
`tenant_isolation` policy keyed on `app.tenant_id`. A new table that forgets this reads fine in
dev and leaks in production.

**Decision.** Every table added in `0002` carries `tenant_id UUID NOT NULL REFERENCES tenants(id)
ON DELETE CASCADE` and is appended to the same `DO $$ ... FOREACH` loop. No exceptions; a table
that genuinely cannot key on `app.tenant_id` must document why, as `tenant_members` does.

**Consequence.** Slightly more boilerplate per table. Tenant isolation stays a property of the
schema rather than a property of everyone's memory.

**Reversed by.** Nothing.
