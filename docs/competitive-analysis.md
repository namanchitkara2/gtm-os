# Competitive analysis

Not a feature checklist. The question is **which workflow each product owns, what it structurally
cannot do, and whether that gap is one we can stand in.** Citations are in `product-research.md`.

## Matrix

| Product | Core strength | Workflow it owns | Major limitation | Opportunity for GTM-OS |
|---|---|---|---|---|
| **Apollo** | Coverage + price; all-in-one | List → sequence | 88% US / **60–73% international** accuracy; 15–35% bounce on exported lists; stale "ghost profiles" | Be the layer that *knows a record is old* and prices the risk, instead of re-buying it |
| **Clay** | Composable waterfalls, 75+ sources | Enrichment orchestration | **42% of negative reviews cite credit burn**; cost unpredictable; a bad waterfall costs more than a flat subscription | Cost-aware routing with a cache check first. BYOK means saving spend is free to us |
| **ZoomInfo** | Enterprise coverage, intent | Account intelligence | $10–50k/yr, annual prepay, add-ons +30–60%; decay it measures itself at 25–30%/yr | Never compete on coverage. Compete on freshness-awareness and on cost of the *next* lookup |
| **Cognism** | EU/phone coverage, compliance posture | Dialling lists | Same per-record economics; regional gaps | Route to it only when confidence demands it; it becomes a tier in our waterfall |
| **6sense** | Intent + AI scoring | Timing / ABM | Users report **opaque scores** they cannot validate, prospects deny the attributed activity | First-party, explainable signals: the change *we* observed, with a snapshot diff to prove it |
| **Instantly** | Sending infra at volume | Inbox rotation, deliverability | Volume is the product, and volume now breaks the channel (0.3% complaint ceiling) | Precision instead of volume; make reply/complaint rate a *budget input*, not a report |
| **Outreach / Salesloft** | Sequence discipline, forecasting | Rep execution | Enterprise-priced, rep-centric, assumes volume is good | Fewer touches, each warranted by evidence; we are upstream of the sequencer |
| **LinkedIn Recruiter** | The graph itself | Candidate search + InMail | Seat cost; **suspends InMail below 13% reply over 100+/14 days** | Protect the user's reply rate as a resource. Our action budget is that protection |
| **Gem** | Post-discovery: outreach, nurture, analytics | Recruiting pipeline | Bound to ATS state; conversations and reasons still decay | `qualification_decisions` + snapshots make rediscovery a query rather than an add-on |
| **Ashby / Greenhouse** | ATS, structured hiring | Hiring process of record | System of record, not an intelligence layer; **75% of ATS records never re-engaged** | Sit beside the ATS and answer "who in here matches the role we just opened?" |
| **SeekOut** | Technical signals off-LinkedIn (GitHub, patents, papers) | Deep technical sourcing | Expensive; needs analyst-grade Boolean users | Natural-language intent → executable workflow, for people who won't write Boolean |
| **hireEZ** | Reach across 45+ sources, multichannel in one place | Volume sourcing | Inconsistent data freshness; accuracy drops on niche/technical titles | Freshness as a visible, first-class field |
| **Attio** | AI-native CRM, hosted MCP (Feb 2026), research agent | Relationship data model | Records are substantially human-entered — it stores what you tell it | We generate the intelligence that would fill it; integrate, don't replace |
| **AI SDR platforms (11x, Fuzzy, et al.)** | Autonomous sending | Outbound volume | **50–70% year-one churn**; 40–60% of pilots dead in 90 days; domain damage | Position against the whole category: autonomy where it is safe, approval where it is not |

## What the matrix says

**1. Coverage is a commodity and a trap.** Four vendors sell overlapping records at a 5–10x price
spread for the same field. Competing there means buying data to resell at a worse margin. The
defensible position is *routing between them* and *remembering what was already bought*.

**2. Nobody owns the middle of the funnel.** Discovery is crowded; sending is crowded. Research,
qualification, shortlist review and re-engagement — the parts that are still spreadsheets and
human judgement — have no owner. That is where the memory layer lives.

**3. Every incumbent's pricing model forbids the obvious feature.** Per-credit vendors cannot ship
a "you don't need this lookup" button. Per-send vendors cannot ship a throttle. Per-seat vendors
cannot ship anything that reduces seats. **BYOK plus a hard platform rate limit means our
incentives point the other way**, and that is a moat made of business model, not code.

**4. "We have an MCP server" is table stakes as of 2026.** Apollo, ZoomInfo, Clay and Attio all
shipped one. The differentiator is not the protocol; it is what the agent finds when it connects:
either an API over a database, or a memory of every decision the team has made.

**5. Regulation is about to make our boring parts valuable.** From 2 Aug 2026 recruitment AI in
the EU must keep logs, explain significant automated decisions, and give a human genuine override
authority. The ledger + approval + decision tables we need anyway become a compliance story that
a pure sourcing tool cannot tell.

## Where we must not compete

- **Data volume.** We will never index 275M contacts and should never imply we do.
- **Sending infrastructure.** Deliverability is a specialist business with real capex.
- **ATS/CRM of record.** Those are switching-cost businesses won years ago.
- **Intent data resale.** The category's credibility is visibly declining, and buying it would
  make our signals as unexplainable as theirs.
