# Bottlenecks

Each entry: **Bottleneck → Who → Current workaround → Why existing tools don't solve it →
GTM-OS solution → Complexity → Value.** Sources are in `product-research.md`; only new
citations are repeated here. Complexity is S/M/L against this codebase as it stands today.

Ordered by value, not by lifecycle position.

---

## B1. Nobody remembers why a decision was made · **P0**
**Who.** Every user, both ICPs. Sharpest for a recruiter re-opening a role they ran last year.
**Workaround.** A "notes" field, a Slack search, or the person who made the call, who has left.
**Why tools don't solve it.** CRMs and ATSs are state machines: they keep the *current* stage and
overwrite the path to it. Apollo/Clay hold no opinion at all — they hand you rows.
**GTM-OS.** `qualification_decisions`: verdict, reason, the evidence ids it rested on, the actor
(model or human), the target it was judged against. Immutable, queryable, replayable.
**Complexity.** S — one table, one write path in the run executor.
**Value.** High. It is the spine for cost (don't re-research what we already rejected), for
trust, and for EU AI Act explainability. Everything else leans on it.

## B2. Enrichment spend is unpredictable and mostly redundant · **P0**
**Who.** GTM engineers and founders paying per credit; 42% of negative Clay reviews.
**Workaround.** Discipline, dashboards, and rebuilding waterfalls after the bill lands.
**Why tools don't solve it.** The vendor is paid by the lookup. A router that says "you already
have this, don't buy it" is directly against the seller's interest.
**GTM-OS.** `packages/providers`: cache-first waterfall with per-field freshness TTLs, a declared
cost and confidence per provider, stop-at-sufficient-confidence, and every spend written to a
ledger with the reason. BYOK means we profit from saving them money.
**Complexity.** M — new package, ledger table, integration in the run executor.
**Value.** High, and it is the clearest thing to put on a pricing page.

## B3. Records go stale silently, and staleness is invisible at the point of use · **P0**
**Who.** Everyone. 22–30%/yr decay, 65.8% of titles change annually.
**Workaround.** Re-buy the list. Bounce, then clean.
**Why tools don't solve it.** A database sells you a row, not a row's age. Confidence is rendered
as a green tick, not as "this title was last confirmed 14 months ago."
**GTM-OS.** Per-field `observed_at` + `source` + `confidence` already implied by `people.doc`;
formalise it as `field_provenance` and surface **age** in every tool return. Never show a fact
without how old it is.
**Complexity.** S/M — the schema half is small, the discipline is in every read path.
**Value.** High. It is also what makes cheap providers usable: you can accept a weaker source
when you can see and price its uncertainty.

## B4. Change detection is either absent or unaffordable · **P1**
**Who.** Sales (job changes = warmest signal there is); recruiters (candidate became available).
**Workaround.** LinkedIn saved searches, manual re-checks, or buying an intent product.
**Why tools don't solve it.** Polling costs money per record per cycle, so vendors either charge
for it or approximate it with third-party intent, which users distrust: "prospects deny the
activities 6sense attributes to them", "flimsy and vague data", scores that feel opaque.
([marketbetter](https://marketbetter.ai/blog/6sense-review-2026/), [burnitdown](https://burnitdown.marketing/intent-data-is-a-lie-and-why-nobody-wants-to-admit-theyve-been-conned))
**GTM-OS.** `entity_snapshots` + a differ. Snapshot what we *already* fetched during normal work,
diff on next legitimate read, and re-read only when a **cheap external signal** (a job posting, a
funding item, a site change) says it is worth one of our ~100 daily actions. No cron over the
watchlist — a budgeted queue, ranked by signal strength.
**Complexity.** M.
**Value.** High, but strictly gated by the action budget. Design it as a queue, not a crawler.

## B5. Outreach volume now destroys the channel it uses · **P0 (as a guardrail)**
**Who.** Anyone sending. AI SDR churn 50–70%; LinkedIn suspends InMail below 13% reply.
**Workaround.** Buy more domains, more seats, more inboxes. Rotate and hope.
**Why tools don't solve it.** Their revenue scales with sends. Throttling is a negative-revenue
feature for a sequencer.
**GTM-OS.** Make reply rate and complaint rate **first-class budget inputs**, not reports. A
channel whose rolling reply rate drops below a floor stops accepting sends and says why. Extend
`action_ledger` with outcomes so the budget can see them.
**Complexity.** M.
**Value.** High and differentiating, precisely because no incumbent will copy it.

## B6. The reviewed shortlist lives in a spreadsheet · **P1**
**Who.** Both ICPs; it is the seam between "the tool found them" and "a human decided."
**Workaround.** Export CSV, review, paste back, lose the reasons (B1) on the way.
**Why tools don't solve it.** Review is a human act the tools model as a status field.
**GTM-OS.** Approval as a first-class state on run steps and actions, with a decision recorded
per item. The spreadsheet exists because the product had nowhere to put a judgement.
**Complexity.** M — we already have the device-code approval precedent and `action_ledger`.
**Value.** High. It is also the EU AI Act "human oversight with authority to override" control.

## B7. Past candidates and past accounts are functionally invisible · **P1**
**Who.** Recruiters most acutely — 75% of ATS records never re-engaged.
**Workaround.** Nothing, mostly. Or a paid rediscovery add-on.
**Why tools don't solve it.** Search is keyword-over-résumé; the *conversation* and the *reason*
were never stored (B1 again), so the match is made against a stale document.
**GTM-OS.** Because memory is the substrate, rediscovery is a query, not a product: people we
have interacted with, with a decision, whose snapshot has since changed, matching a new target.
**Complexity.** S once B1/B3/B4 exist.
**Value.** High for recruiting; it is the cheapest possible source of qualified pipeline —
zero enrichment spend, zero new LinkedIn reads.

## B8. Research is re-done per rep, per quarter, per role · **P1**
**Who.** Teams. Invisible to a solo user until the second seat appears.
**Workaround.** Ask in Slack. Redo it.
**Why tools don't solve it.** Research output lands in a message or a doc, not on the entity.
**GTM-OS.** Research artefacts attach to the entity with provenance and a TTL, and the next run
reads them before spending.
**Complexity.** S/M.
**Value.** Medium-high; compounds with seats.

## B9. "Personalisation" is now pattern-matched and ignored · **P1**
**Who.** Anyone sending. Buyers skip the "I noticed you recently [trigger]" opener.
**Workaround.** More tokens, more variables, worse results.
**Why tools don't solve it.** They generate from a template over thin data, and the model will
assert whatever it needs to fill the slot.
**GTM-OS.** **A claim in a message must cite an evidence id.** No evidence, no claim — the drafter
refuses rather than invents. This is enforceable at the schema level because §B3 gives every fact
a source.
**Complexity.** M.
**Value.** High, and it is the honest version of the personalisation pitch.

## B10. Handoffs between tools lose everything but the row · **P2**
**Who.** RevOps. >1 in 10 CRM records are duplicates; ~2/3 of required fields empty.
([default](https://www.default.com/post/crm-data-hygiene))
**Workaround.** The Monday-morning spreadsheet, 2–3 hours a week.
**Why tools don't solve it.** Each product's export is the lowest common denominator: a flat row.
**GTM-OS.** Be the layer that holds context, and sync *projections* outward rather than trying to
be the system of record for everyone.
**Complexity.** L.
**Value.** Medium now, high at team scale. Explicitly after the core works.

---

## Lifecycle coverage check

**Sales.** ICP definition `targets` (exists) · account discovery (P2) · person discovery (exists)
· enrichment **B2** · research **B8** · qualification **B1** · segmentation (P2) ·
personalisation **B9** · outreach **B5** · follow-up **B5** · response handling (P1, needs an
inbox source) · meeting (P2) · CRM **B10** · pipeline (P2) · analytics (P1) · learning (P2).

**Recruiting.** Role definition `targets` (exists) · candidate discovery (exists) · research
**B8** · qualification **B1** · shortlisting **B6** · personalisation **B9** · outreach **B5** ·
follow-up **B5** · interview (out of scope, that is an ATS) · evaluation **B1** · pipeline (P2) ·
hiring (out of scope). **Rediscovery B7 is the wedge** — it is the one stage where we start with
an advantage rather than parity.

**Deliberately out of scope:** interview scheduling, assessments, contracts, invoicing. An ATS
and a CRM exist; we are the intelligence layer across them, not a replacement for them.
