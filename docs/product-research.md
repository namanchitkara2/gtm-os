# GTM-OS product research

Written 2026-09-18, before migration 0002. Everything labelled **Evidence** was read during
this pass and is cited. Everything labelled **Hypothesis** was not — it is a belief we hold,
and it should be tested, not designed around as if it were a fact.

The question this pass was trying to answer: *what are the biggest bottlenecks in modern
recruiting and sales workflows, and what should GTM-OS do about them that the incumbents
structurally cannot?*

---

## 1. The finding that reframes the product

Outbound has an economics problem, not a tooling problem.

- **Evidence.** Average cold-email reply rates fell from ~8.5% (2019) to ~3.4% (2026). Per-rep
  monthly volume rose from ~1,150 to ~7,400 after AI adoption while raw reply rates fell from
  4.7% to 2.9%. ([genflows](https://www.genflows.com/blog/death-of-the-sdr-what-2026-data-says),
  [digitalapplied 100k-email analysis](https://www.digitalapplied.com/blog/ai-sdr-real-performance-100k-email-analysis-2026))
- **Evidence.** AI SDR tools churn at **50–70% in year one**; 40–60% of pilots are paused or
  killed inside 90 days. The two stated reasons are reply rates below vendor projections and
  domain-reputation damage. ([rework](https://resources.rework.com/news/sales-tech/ai-sdr-worth-it-2026-hybrid-model-sales-leader))
- **Evidence.** Gmail/Yahoo/Microsoft enforce a **0.3% spam-complaint ceiling** (target 0.1%)
  with SPF+DKIM+DMARC required over 5,000/day, and since Nov 2025 Google moved non-compliant
  traffic from temporary 4.7.x deferrals toward permanent 5.7.x rejections.
  ([redsift](https://redsift.com/guides/bulk-email-sender-requirements), [powerdmarc](https://powerdmarc.com/bulk-email-sender-requirements/))

Read together: **volume is now a liability and the marginal cost of "personalisation" has gone
to zero, so personalisation is no longer a differentiator.** Any product whose pitch is "send
more, faster" is selling into a market that measurably punishes it.

What survives that squeeze is *precision*: fewer, better-timed, better-warranted touches. That
is a claim about **knowing things**, not about sending things — and knowing things is a memory
and provenance problem. This is the product.

## 2. The second finding: the data underneath everyone is rotting faster than it is refreshed

- **Evidence.** B2B contact data decays ~2.1%/month, ~22.5%/year; ZoomInfo's own analysis puts
  it at 25–30%/yr; **65.8% of job titles change annually** and 70.8% of contacts see at least one
  change in 12 months. Tech/SaaS 25–35%, VC-backed startups 30–40%.
  ([cleanlist](https://www.cleanlist.ai/blog/2026-01-22-b2b-data-decay-statistics), [lusha](https://www.lusha.com/blog/b2b-data-decay-rate-measured/), [zoominfo](https://pipeline.zoominfo.com/marketing/b2b-data-decay))
- **Evidence.** Apollo's accuracy is ~88% US and **60–73% international**; users report 15–35%
  bounce rates on exported lists *even on contacts marked verified*, and "ghost profiles" with
  titles 12–24 months stale, worst in non-US markets and small companies.
  ([prospeo](https://prospeo.io/s/apolloio-reviews), [mailreach](https://www.mailreach.co/blog/apollo-io-review))

The industry's answer is to re-buy the same record. The structural answer is to **hold a
timestamped history per field and know when you are looking at something old** — which is the
same table that answers "what changed about this account since we last looked?"

## 3. The third finding: cost is unpredictable, and that is what users actually complain about

- **Evidence.** **42% of negative Clay reviews mention credit burn**; users report $800 in a week
  while still learning; multi-step waterfalls plus Actions exhaust monthly limits early and a
  badly-built waterfall can cost more per record than a flat subscription.
  ([cleanlist](https://www.cleanlist.ai/blog/2026-03-12-clay-pricing-changes-2026), [syncgtm](https://syncgtm.com/blog/clay-review))
- **Evidence.** Price points to route against: Clay $185/mo for 10k credits, $495/mo for 25k;
  Apollo 2 credits per email and **8 per phone**; inside Clay, Apollo costs 1–2 credits/lookup
  against ZoomInfo's 5–10; ZoomInfo contracts $10–15k/yr small team, $25–50k mid-market, with
  add-ons another 30–60%. ([cleanlist](https://www.cleanlist.ai/blog/2026-03-12-clay-pricing-changes-2026), [formanorden](https://formanorden.com/blog/clay-vs-apollo-vs-zoominfo/))

A 5–10x spread between the cheapest and most expensive source of the *same field* is a routing
problem with a correct answer. Nobody sells the router, because every vendor in that list is
paid by the lookup. **We are not** — BYOK means the customer pays the provider directly, so
saving them money costs us nothing and is a pitch no incumbent can copy without cannibalising
itself.

## 4. Recruiting: the bottleneck is after discovery, and the memory is already lost

- **Evidence.** "The bottleneck for most recruiting teams in 2026 is not finding people — it is
  what happens after"; recruiters spend **over a third of their week on manual sourcing tasks**.
  ([sitepoint](https://www.sitepoint.com/best-sourcing-tools-for-recruiters/))
- **Evidence.** **75% of ATS records contain viable candidates never re-engaged**; teams cannot
  re-engage them because ATS search is poor, profile data is stale, and *past conversations are
  lost*. ([thehirehub](https://www.thehirehub.ai/blog/talent-rediscovery-ai-silver-medalists-ats-2026), [gem](https://www.gem.com/blog/silver-medalists-candidate-rediscovery))
- **Evidence.** LinkedIn messages return **16.4% candidate reply vs 4.9% email** in tracked
  recruiter sequences; realistic InMail target is 12–18%; **LinkedIn warns and can suspend
  InMail below 13% response over 100+ sends in 14 days**.
  ([pin](https://www.pin.com/blog/ai-linkedin-outreach-response-rate/), [elvatix](https://elvatix.com/en/blog/good-inmail-response-rate-recruiters))

That last number is the whole thesis in one line: **the platform itself punishes low-precision
outreach.** A recruiter's reply rate is a resource to be protected, exactly like domain
reputation. Both argue for the same product: fewer sends, each one warranted by something we
can point at.

## 5. The regulatory clock nobody in this space is building for

- **Evidence.** EU AI Act high-risk obligations for recruitment AI apply from **2 August 2026**:
  keep logs, designate trained human reviewers *with genuine authority to override* ("a rubber
  stamp is not oversight"), prepare explanations for significant automated decisions, inform
  candidates they are interacting with AI, run adverse-impact analysis on your own candidate
  data. Scope explicitly covers filtering applications and evaluating candidates.
  ([accessfinancial](https://accessfinancial.com/eu-ai-act-recruitment-high-risk-hiring-2026/), [artificialintelligenceact.eu](https://artificialintelligenceact.eu/what-the-act-means-for-staffing-businesses/))

An AI that shortlists candidates must be able to answer *why* and show *who approved it*. That
is not a compliance bolt-on; it is a schema decision, and it is the same schema as "why did we
qualify this account?" We should build it once and sell it to both ICPs.

## 6. What the AI-native cohort has already settled

- **Evidence.** Apollo, ZoomInfo, Clay and Attio all shipped MCP servers in 2026; Attio's hosted
  MCP landed Feb 2026; Clay's is a remote control for their platform on higher tiers only.
  The emerging definition of "AI-native" includes documented schema endpoints, BYO-model,
  **action budgets per agent, scoped permissions, and audit trails of agent actions**.
  ([pipe0](https://www.pipe0.com/blog/best-mcp-servers-gtm), [automationjinn](https://www.automationjinn.com/blog/attio-mcp-claude-ai-agents), [wetheflywheel](https://wetheflywheel.com/en/guides/best-ai-native-crm-2026/))

So "we have an MCP server" is now table stakes, not a moat. What is *not* commoditised: an
action budget that is enforced because a real-world rate limit forces it, and a memory layer
the agent reads before it spends anything.

## 7. Where this leaves GTM-OS

Our existing constraint — **one LinkedIn action at a time per user, ~80–150 reads/day, the cache
*is* the throughput** — reads like a limitation next to Apollo's 275M contacts. Against the 2026
evidence it is a fit:

| The market's problem | Our constraint, restated |
|---|---|
| Volume destroys reply rates and domains | We physically cannot do volume |
| Credit burn is unpredictable | We hold no credits; BYOK + routing makes spend visible |
| Data is stale and re-bought | Scarce reads force a cache, and a cache with timestamps is memory |
| Regulators want logs and override authority | Every action already goes through a ledger |

**The product is the intelligence and orchestration layer, priced on precision rather than
volume.** Not "Apollo + Clay + LinkedIn + AI."

## 8. The 15 questions, answered briefly

1. *What does each product solve?* Databases (Apollo/ZoomInfo/Cognism) sell coverage; Clay sells
   composability; Gem/Ashby/Greenhouse sell pipeline; SeekOut/hireEZ sell reach; 6sense sells timing.
2. *What workflow does it optimise?* Everyone optimises list-building and sending. Almost nobody
   optimises *deciding* and *remembering*.
3. *What is still manual?* Qualification judgement, research synthesis, re-engagement timing.
4. *Where do workflows break?* At tool boundaries — the export/import seam.
5. *What is repetitive?* Re-researching the same account for a new rep, new quarter, new role.
6. *What data gets lost?* Reasons. Every tool keeps the outcome and discards why.
7. *Where is copy/paste?* CSV between enrichment, sequencer and CRM (Evidence §GTM engineering).
8. *Where is enrichment expensive?* Phones (8 credits vs 2), and any waterfall run twice.
9. *Where is automation unreliable?* Anything asserting a fact about a person from an LLM.
10. *Where is human judgement needed?* Qualification, and any first-touch that names a person.
11. *Where is AI low-confidence?* Inference from absence — "they're probably hiring because…"
12. *Why do users switch tools?* Because memory does not travel between them.
13. *What should persist and doesn't?* Objections, rejection reasons, past conversations, why-not.
14. *What is hard to audit?* Which agent did what, on whose approval, using which evidence.
15. *What is still spreadsheet-driven?* Everything in the middle: the reviewed shortlist.
