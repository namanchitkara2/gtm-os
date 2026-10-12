# Feature roadmap

Derived from `bottlenecks.md`. Each item carries the required block. **Complexity** is S/M/L
against this repo today. Nothing here is scheduled by date, because the ordering is what matters:
**P0 must be a working system, not a scaffold.**

The one-line rule this roadmap obeys: *the platform can perform roughly 100 LinkedIn actions per
user per day, forever. Every feature either respects that budget or is redesigned until it does.*

---

## P0 — Core. The product is not itself without these.

### P0.1 Persistent GTM memory (people, companies, interactions, decisions, evidence)
- **Problem.** Nothing survives a run. Reasons, conversations, and prior judgements are lost (B1).
- **Evidence.** 75% of ATS records never re-engaged, partly because "past conversations may be
  lost"; CRMs keep state and discard the path to it.
- **User.** Both ICPs; sharpest for a recruiter reopening a role.
- **Current workaround.** Notes fields, Slack search, the person who left.
- **GTM-OS solution.** `interactions`, `qualification_decisions`, `research_artifacts`,
  `field_provenance` — all tenant-scoped, all immutable-by-default, all referencing evidence ids.
- **Why now.** Retrofitting provenance onto a live data model is the single most expensive
  migration this product could ever be asked to do.
- **Complexity.** M. **Priority.** P0.

### P0.2 Cost-aware provider routing (`packages/providers`)
- **Problem.** Enrichment spend is unpredictable and largely redundant (B2).
- **Evidence.** 42% of negative Clay reviews cite credit burn; $800/week reported while learning;
  Apollo 2 credits/email vs 8/phone; 1–2 credits/lookup vs ZoomInfo's 5–10 for the same field.
- **User.** Whoever pays the provider bill — under BYOK, always the customer.
- **Current workaround.** Rebuild the waterfall after the invoice.
- **GTM-OS solution.** Cache → freshness check → cheapest sufficient provider → stop at target
  confidence → escalate only if required. Every attempt written to `enrichment_ledger` with cost,
  confidence and outcome.
- **Why now.** It is the foundation every other feature spends through; adding it later means
  rewriting every call site.
- **Complexity.** M. **Priority.** P0.

### P0.3 Freshness and provenance surfaced at the point of use
- **Problem.** Staleness is invisible where it matters — in the tool's return value (B3).
- **Evidence.** 22–30%/yr decay; 65.8% of titles change annually; 15–35% bounce on "verified".
- **User.** The agent deciding whether to spend an action, and the human reading the output.
- **Current workaround.** A green "verified" tick that means nothing about age.
- **GTM-OS solution.** Every fact carries `source`, `observed_at`, `confidence`; every MCP tool
  return includes field age; stale-beyond-TTL fields are marked, not silently served.
- **Why now.** Same migration as P0.1.
- **Complexity.** S/M. **Priority.** P0.

### P0.4 Approval states and policy (human-in-the-loop)
- **Problem.** Human review happens in a spreadsheet because the product has nowhere to put a
  judgement (B6).
- **Evidence.** EU AI Act, from 2 Aug 2026: trained reviewers with genuine override authority,
  logs, explanations for significant automated decisions.
- **User.** Recruiters in the EU (hard requirement); everyone else (trust).
- **Current workaround.** Export, review, paste back.
- **GTM-OS solution.** `approval_policies` per tenant × action class; `approvals` rows with actor,
  verdict, reason; run steps that can enter `awaiting_approval` and resume. Low-risk enrichment
  stays automatic — the policy decides, not the code path.
- **Why now.** The deadline is already here, and the device-code approval flow gives us the
  pattern to copy.
- **Complexity.** M. **Priority.** P0.

### P0.5 Budget guardrails that see outcomes
- **Problem.** Volume destroys the channel it uses (B5).
- **Evidence.** 0.3% complaint ceiling with 5.7.x permanent rejections since Nov 2025; LinkedIn
  suspends InMail below 13% reply over 100+ sends/14 days; AI SDR churn 50–70%.
- **User.** Every sender; their domain and LinkedIn account are the assets at risk.
- **Current workaround.** More domains, more inboxes, rotation.
- **GTM-OS solution.** `action_outcomes` feeding a rolling reply/complaint rate per channel;
  budget refuses sends below a floor and says why. Existing `daily_bridge_budget` becomes one
  input among several.
- **Why now.** We are about to build autonomous sending (Phase 3). The guardrail must precede it.
- **Complexity.** M. **Priority.** P0.

### P0.6 Evidence-bound drafting
- **Problem.** AI personalisation fabricates, and buyers now pattern-match it anyway (B9).
- **Evidence.** Reply rates fell as AI volume rose; the "[trigger event]" opener is recognised
  and skipped.
- **User.** Both ICPs.
- **Current workaround.** More variables in the template.
- **GTM-OS solution.** A drafted message stores `claims[]`, each with an `evidence_id`. A claim
  without evidence is refused at write time, not warned about in a prompt.
- **Why now.** Cheap while the drafter is small; unenforceable once messages are generated in
  five places.
- **Complexity.** M. **Priority.** P0.

---

## P1 — Important. Significantly better product; not load-bearing.

### P1.1 Change detection by snapshot diff
- **Problem.** "What changed since we last looked?" is unanswerable (B4).
- **Evidence.** Job changes are the largest decay driver; intent vendors' scores are distrusted.
- **GTM-OS solution.** `entity_snapshots` written on every legitimate read; a differ producing
  typed `signals` (title_change, company_change, headcount_move, tech_added).
- **Why now.** Snapshots must start accumulating before the diff is useful — the data is the lead
  time, so begin writing it early even if the differ ships later.
- **Complexity.** M. **Priority.** P1 (schema in P0's migration).

### P1.2 Signals and trigger-based workflows
- **Problem.** "Something changed" never becomes "contact this person" (B4).
- **GTM-OS solution.** `signals` table with source, strength, decay; a budgeted, ranked queue that
  proposes a re-read or an outreach opportunity. **Not a cron over a watchlist** — the action
  budget makes polling impossible, so signals must come from cheap external sources first.
- **Complexity.** M/L. **Priority.** P1.

### P1.3 Talent and account rediscovery
- **Problem.** Past candidates and accounts are invisible (B7).
- **Evidence.** 75% of ATS records contain viable candidates never re-engaged; silver medallists
  convert better than fresh sourcing.
- **GTM-OS solution.** A query over memory: interacted + decided + snapshot-changed + matches a
  new target. Zero enrichment spend, zero new LinkedIn reads.
- **Why now.** It is the cheapest pipeline in the product and the clearest recruiting wedge.
- **Complexity.** S once P0.1 lands. **Priority.** P1.

### P1.4 Research artefacts with TTL
- **Problem.** Research is redone per rep, per quarter (B8).
- **GTM-OS solution.** Attach research to the entity with provenance and expiry; runs read before
  they spend.
- **Complexity.** S/M. **Priority.** P1.

### P1.5 GTM analytics that are cost-per-outcome
- **Problem.** Vanity metrics (opens, sends) hide the economics.
- **GTM-OS solution.** source→qualified, qualified→reply, reply→meeting/interview, and **cost per
  qualified prospect** and **cost per meeting**, computed off the enrichment and action ledgers.
- **Complexity.** M. **Priority.** P1.

### P1.6 Natural-language intent → executable workflow (the copilot)
- **Problem.** SeekOut-class power needs analyst-grade Boolean; most users won't.
- **GTM-OS solution.** The MCP client *is* the copilot — we do not build a chat UI. We expose
  `plan_workflow` returning a typed, inspectable plan with an estimated cost and action count,
  which the user's own agent renders and the human approves.
- **Why now.** It is mostly schema and tool surface; the LLM is already in the room.
- **Complexity.** M. **Priority.** P1.

---

## P2 — Expansion. After the core is working and used.

- **P2.1 CRM/ATS projections outward** (B10). Sync a projection to Attio/Greenhouse; never try to
  be their system of record. **L.**
- **P2.2 Account discovery and segmentation.** Today targets are person-shaped. **M.**
- **P2.3 Response handling and objection memory.** Needs an inbox source; high value, real scope.
- **P2.4 Multi-seat team memory.** Shared research, per-seat budgets, "who last touched this".
- **P2.5 Visual workflow builder.** **Explicitly deferred.** The brief says not to build the
  editor unless research validates it, and research did not: the MCP client already gives users a
  programmable surface. Revisit only if non-technical buyers become the ICP.

---

## Experimental — interesting, unvalidated

- **E1. Reply-rate-aware send scheduling.** Learn per-persona timing from our own outcomes.
  Risk: too little data per tenant to beat a simple heuristic.
- **E2. Cross-tenant anonymised freshness.** If one tenant observes a title change, every tenant's
  copy of that fact is *marked stale* (never populated). Enormous value, serious privacy design.
  Do not build without an explicit legal read.
- **E3. Warm-path scoring.** Rank by relationship distance, not just fit.
- **E4. Automatic waterfall tuning.** Learn provider order per segment from the ledger.

---

## What changed in the plan because of research

1. **Memory and the intelligence graph merged into one object.** Two designs were planned; one
   ships. The graph is edges over the memory tables, not a separate store.
2. **Provider routing moved from P1 to P0.** It is the foundation everything else spends through.
3. **Change detection was redesigned from a crawler into a budgeted queue** because ~100
   actions/day/user makes polling arithmetically impossible.
4. **The workflow builder was demoted to P2** and the copilot reframed as a tool returning a plan,
   not a UI we own.
5. **Approval became P0 rather than a later trust feature**, because 2 Aug 2026 made it a
   regulatory requirement for one of our two ICPs.
