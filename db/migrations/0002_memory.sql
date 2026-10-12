-- GTM-OS memory layer.
--
-- 0001 gave this product state: who exists, what ran, what was sent. This
-- migration gives it memory: what we observed, when, from where, why we decided
-- what we decided, and what it cost to find out.
--
-- Three rules, all of them expensive to retrofit (see docs/architecture-decisions.md):
--
-- 1. ADR-007 — every asserted fact carries source + observed_at + confidence.
--    A claim in an outbound message references an evidence row, or it does not
--    get written. "Never fabricate personalisation" is a constraint here, not a
--    sentence in a prompt.
-- 2. ADR-006 — memory and the intelligence graph are one object. people and
--    companies stay the nodes; entity_edges holds the relationships.
-- 3. ADR-013 — every table below is tenant-scoped and joins the RLS loop at the
--    bottom. A table that forgets reads fine in dev and leaks in production.

-- ------------------------------------------------------------- provenance --

-- One row per observed fact about an entity. This is the table the drafter
-- points at when it makes a claim, and the table freshness is computed from.
-- It is append-only by convention: a newer observation supersedes an older one
-- rather than updating it, because the history is what change detection reads.
CREATE TABLE field_provenance (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    field        TEXT NOT NULL,              -- 'title' | 'email' | 'headcount' | ...
    value        JSONB NOT NULL,
    source       TEXT NOT NULL,              -- 'linkedin' | 'apollo' | 'user' | 'inference' | ...
    -- 0..1. An inference is allowed to be low; it is not allowed to be unlabelled.
    confidence   NUMERIC(3,2) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL,
    superseded_by UUID REFERENCES field_provenance(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX field_provenance_current_idx
    ON field_provenance (tenant_id, entity_type, entity_id, field, observed_at DESC)
    WHERE superseded_by IS NULL;

-- The unit a message claim, a qualification decision, or a signal points at.
-- Evidence is never rewritten; a correction is a new row.
CREATE TABLE evidence (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    kind         TEXT NOT NULL,              -- 'profile_fact' | 'job_posting' | 'news' | 'interaction' | ...
    body         TEXT NOT NULL,              -- the quotable thing, in the words we can defend
    source       TEXT NOT NULL,
    source_url   TEXT,
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX evidence_entity_idx ON evidence (tenant_id, entity_type, entity_id, observed_at DESC);

-- --------------------------------------------------------------- the graph --

-- ADR-006. Typed, directional, tenant-scoped. Postgres because our queries are
-- one and two hops with a hard tenant filter, which an index serves exactly.
CREATE TABLE entity_edges (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    src_type    TEXT NOT NULL CHECK (src_type IN ('person','company','target','run','campaign')),
    src_id      UUID NOT NULL,
    edge_type   TEXT NOT NULL,   -- 'works_at' | 'worked_at' | 'reports_to' | 'uses_tech' | 'sourced_by' | ...
    dst_type    TEXT NOT NULL CHECK (dst_type IN ('person','company','target','run','campaign')),
    dst_id      UUID NOT NULL,
    properties  JSONB NOT NULL DEFAULT '{}'::jsonb,
    source      TEXT NOT NULL,
    confidence  NUMERIC(3,2) NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to    TIMESTAMPTZ,     -- NULL = still true. Set it rather than deleting: "worked_at" is history.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, src_type, src_id, edge_type, dst_type, dst_id, observed_at)
);
CREATE INDEX entity_edges_out_idx ON entity_edges (tenant_id, src_type, src_id, edge_type)
    WHERE valid_to IS NULL;
CREATE INDEX entity_edges_in_idx  ON entity_edges (tenant_id, dst_type, dst_id, edge_type)
    WHERE valid_to IS NULL;

-- ------------------------------------------------------------ interactions --

-- Every touch, in either direction, on any channel. This is the table that makes
-- "we already spoke to them in March, and they said no because of pricing"
-- a query instead of a memory.
CREATE TABLE interactions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    person_id   UUID REFERENCES people(id) ON DELETE CASCADE,
    company_id  UUID REFERENCES companies(id) ON DELETE SET NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
    channel     TEXT NOT NULL CHECK (channel IN ('linkedin_dm','linkedin_invite','email','call','meeting','other')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    subject     TEXT,
    body        TEXT,
    -- Set on inbound once classified. Kept separate from body so a reclassification
    -- never rewrites what the person actually said.
    sentiment   TEXT CHECK (sentiment IN ('positive','neutral','negative','out_of_office','unsubscribe')),
    objection   TEXT,                       -- 'price' | 'timing' | 'incumbent' | 'not_hiring' | free text
    action_id   UUID REFERENCES action_ledger(id) ON DELETE SET NULL,
    run_id      UUID REFERENCES runs(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX interactions_person_idx ON interactions (tenant_id, person_id, occurred_at DESC);
CREATE INDEX interactions_company_idx ON interactions (tenant_id, company_id, occurred_at DESC);

-- --------------------------------------------------------------- decisions --

-- B1. The spine. Why we qualified, who decided, and what they were looking at.
-- Immutable: a changed mind is a new row, and the pair is the interesting part.
CREATE TABLE qualification_decisions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    target_id    UUID REFERENCES targets(id) ON DELETE SET NULL,
    verdict      TEXT NOT NULL CHECK (verdict IN ('qualified','disqualified','deferred','needs_research')),
    reason       TEXT NOT NULL,
    -- ADR-007: what the verdict rested on. An empty array is allowed only for
    -- a human verdict, where the human is the evidence.
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    actor_type   TEXT NOT NULL CHECK (actor_type IN ('model','human','rule')),
    actor_id     TEXT,                      -- user id, model name, or rule name
    confidence   NUMERIC(3,2) CHECK (confidence >= 0 AND confidence <= 1),
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT model_verdicts_need_evidence
        CHECK (actor_type <> 'model' OR cardinality(evidence_ids) > 0)
);
CREATE INDEX qual_decisions_entity_idx
    ON qualification_decisions (tenant_id, entity_type, entity_id, decided_at DESC);
CREATE INDEX qual_decisions_target_idx ON qualification_decisions (tenant_id, target_id, decided_at DESC);

-- Research output attached to the entity instead of dying in a chat window.
CREATE TABLE research_artifacts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    kind         TEXT NOT NULL,             -- 'summary' | 'tech_stack' | 'org_map' | 'news_digest'
    body         TEXT NOT NULL,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    produced_by  TEXT NOT NULL,             -- model name or 'human'
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- P1.4. Past this, a run re-researches instead of trusting the cache.
    expires_at   TIMESTAMPTZ
);
CREATE INDEX research_entity_idx ON research_artifacts (tenant_id, entity_type, entity_id, created_at DESC);

-- ------------------------------------------------- snapshots and signals --

-- ADR-009. Written on every read we were already making. The diff between two
-- snapshots is where a signal comes from; it costs no extra LinkedIn action.
CREATE TABLE entity_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    doc          JSONB NOT NULL,
    -- Digest of the fields we diff on, so an unchanged re-read is one comparison
    -- rather than a JSONB walk.
    digest       TEXT NOT NULL,
    source       TEXT NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX entity_snapshots_idx
    ON entity_snapshots (tenant_id, entity_type, entity_id, captured_at DESC);

-- "Something changed" or "something happened", typed and decaying. Produced by
-- the snapshot differ or by a cheap external source; never bought as intent.
CREATE TABLE signals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    signal_type  TEXT NOT NULL,   -- 'title_change'|'company_change'|'funding'|'job_posting'|'tech_added'|'headcount_jump'
    strength     NUMERIC(3,2) NOT NULL DEFAULT 0.5 CHECK (strength >= 0 AND strength <= 1),
    summary      TEXT NOT NULL,
    evidence_ids UUID[] NOT NULL DEFAULT '{}',
    source       TEXT NOT NULL,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A signal is news, and news goes stale. Past this it stops ranking.
    expires_at   TIMESTAMPTZ,
    acted_on_at  TIMESTAMPTZ,
    UNIQUE (tenant_id, entity_type, entity_id, signal_type, detected_at)
);
CREATE INDEX signals_live_idx ON signals (tenant_id, strength DESC, detected_at DESC)
    WHERE acted_on_at IS NULL;

-- ADR-009's queue. Entities worth spending a scarce LinkedIn action to re-read,
-- ranked, drained by whatever daily budget is left after real work.
CREATE TABLE refresh_queue (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID NOT NULL,
    reason       TEXT NOT NULL,
    priority     NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    queued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
-- One *pending* refresh per entity, not one ever: the queue drains and refills,
-- and a plain UNIQUE would lock every entity out permanently after its first
-- refresh completed. That failure is invisible — change detection would simply
-- stop covering everyone it had already looked at once.
CREATE UNIQUE INDEX refresh_queue_one_pending_idx
    ON refresh_queue (tenant_id, entity_type, entity_id)
    WHERE completed_at IS NULL;
CREATE INDEX refresh_queue_pending_idx ON refresh_queue (tenant_id, priority DESC, queued_at)
    WHERE completed_at IS NULL;

-- ------------------------------------------------------- spend and routing --

-- ADR-008. Every enrichment attempt, hit or miss, cheap or premium. This is both
-- the cost report and the training data for waterfall ordering.
CREATE TABLE enrichment_ledger (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('person','company')),
    entity_id    UUID,
    field        TEXT NOT NULL,
    provider     TEXT NOT NULL,             -- 'cache' is a provider, and the cheapest one
    outcome      TEXT NOT NULL CHECK (outcome IN ('hit','miss','error','skipped_cache','skipped_budget')),
    -- Credits in the provider's own unit; USD where we can compute it. Both are
    -- kept because a credit's price changes and a spent credit does not.
    credits      NUMERIC(10,3) NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(10,4) NOT NULL DEFAULT 0,
    confidence   NUMERIC(3,2) CHECK (confidence >= 0 AND confidence <= 1),
    latency_ms   INT,
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX enrichment_ledger_cost_idx ON enrichment_ledger (tenant_id, created_at DESC);
CREATE INDEX enrichment_ledger_provider_idx ON enrichment_ledger (tenant_id, provider, field, outcome);

-- Per-tenant freshness policy. A phone number is worth re-buying after a year;
-- a job title in a startup is stale in a quarter (30-40%/yr decay, docs/product-research.md).
CREATE TABLE field_policies (
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    field        TEXT NOT NULL,
    ttl_days     INT NOT NULL,
    -- Stop the waterfall as soon as a provider returns at least this.
    min_confidence NUMERIC(3,2) NOT NULL DEFAULT 0.7
                   CHECK (min_confidence >= 0 AND min_confidence <= 1),
    max_cost_usd NUMERIC(10,4),             -- NULL = no ceiling beyond the plan's
    PRIMARY KEY (tenant_id, field)
);

-- ------------------------------------------------------ approvals (ADR-010) --

CREATE TABLE approval_policies (
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_class TEXT NOT NULL,             -- 'outbound_message'|'connection_request'|'enrichment'|'research'|'refresh_read'
    mode         TEXT NOT NULL CHECK (mode IN ('auto','require_approval','forbid')),
    -- Optional ceiling above which even an 'auto' class must be approved.
    auto_limit_per_day INT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, action_class)
);

CREATE TABLE approvals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_class TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('run_step','action','draft','run')),
    subject_id   UUID NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','expired')),
    -- EU AI Act: a reviewer with genuine authority to override, and a record of it.
    decided_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    decided_at   TIMESTAMPTZ,
    reason       TEXT,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- what is being approved, rendered for a human
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ
);
CREATE INDEX approvals_pending_idx ON approvals (tenant_id, status, requested_at)
    WHERE status = 'pending';

-- ------------------------------------------------- drafts and their claims --

-- ADR-007 enforced in the schema: a model-authored claim without an evidence id
-- cannot be written, so a message cannot assert something we cannot show.
CREATE TABLE message_drafts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    person_id    UUID NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    channel      TEXT NOT NULL CHECK (channel IN ('linkedin_dm','linkedin_invite','email')),
    subject      TEXT,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','awaiting_approval','approved','rejected','sent','discarded')),
    produced_by  TEXT NOT NULL,
    run_id       UUID REFERENCES runs(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX message_drafts_person_idx ON message_drafts (tenant_id, person_id, created_at DESC);

CREATE TABLE draft_claims (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    draft_id     UUID NOT NULL REFERENCES message_drafts(id) ON DELETE CASCADE,
    claim        TEXT NOT NULL,
    -- NOT NULL is the whole point. No evidence, no claim, no message.
    evidence_id  UUID NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX draft_claims_draft_idx ON draft_claims (tenant_id, draft_id);

-- ---------------------------------------------------- outcomes (ADR-011) --

-- What happened after we sent. Feeds the budget, so the platform can refuse to
-- send when a channel's reply rate is heading for a suspension.
CREATE TABLE action_outcomes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_id    UUID REFERENCES action_ledger(id) ON DELETE CASCADE,
    person_id    UUID REFERENCES people(id) ON DELETE SET NULL,
    channel      TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN
                 ('replied','accepted','ignored','bounced','complained','unsubscribed','failed')),
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX action_outcomes_window_idx ON action_outcomes (tenant_id, channel, observed_at DESC);

-- ------------------------------------------------------- row-level security --
-- ADR-013. Same pattern as 0001: enable, force, isolate on app.tenant_id.

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'field_provenance','evidence','entity_edges','interactions',
    'qualification_decisions','research_artifacts','entity_snapshots','signals',
    'refresh_queue','enrichment_ledger','field_policies','approval_policies',
    'approvals','message_drafts','draft_claims','action_outcomes'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING '
      '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
  END LOOP;
END $$;

-- ----------------------------------------------------------------- defaults --
-- Conservative by default, per ADR-010: reads and enrichment run themselves,
-- anything that reaches a human being does not.

INSERT INTO approval_policies (tenant_id, action_class, mode)
SELECT id, c.action_class, c.mode
FROM tenants,
     (VALUES ('outbound_message','require_approval'),
             ('connection_request','require_approval'),
             ('enrichment','auto'),
             ('research','auto'),
             ('refresh_read','auto')) AS c(action_class, mode)
ON CONFLICT DO NOTHING;

-- Decay rates from docs/product-research.md: titles are the fastest-moving field
-- (65.8%/yr), emails 20-30%/yr, phones 15-20%/yr.
INSERT INTO field_policies (tenant_id, field, ttl_days, min_confidence)
SELECT id, f.field, f.ttl_days, f.min_conf
FROM tenants,
     (VALUES ('title', 90, 0.70),
             ('company', 90, 0.70),
             ('email', 180, 0.80),
             ('phone', 270, 0.80),
             ('headcount', 120, 0.60),
             ('tech_stack', 120, 0.60)) AS f(field, ttl_days, min_conf)
ON CONFLICT DO NOTHING;
