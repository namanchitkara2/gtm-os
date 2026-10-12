-- GTM-OS initial schema.
--
-- Two rules this schema exists to enforce, both of which are expensive to
-- retrofit and cheap to write now:
--
-- 1. Every customer-owned row carries tenant_id and is covered by row-level
--    security. A multi-tenant product that relies on every query remembering a
--    WHERE clause leaks the first time someone forgets one.
-- 2. Nothing here ever stores a LinkedIn cookie or a provider API key. The
--    bridge holds the former on the user's own machine; Secrets Manager holds
--    the latter and this schema stores only the ARN.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "citext";

-- ---------------------------------------------------------------- tenancy --

CREATE TABLE tenants (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    slug         CITEXT NOT NULL UNIQUE,
    plan         TEXT NOT NULL DEFAULT 'trial',
    -- Conservative by default. Raising this is a deliberate act, because it is
    -- the number that decides whether a customer's LinkedIn account survives.
    daily_bridge_budget INT NOT NULL DEFAULT 100,
    tos_accepted_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Cognito `sub`. The identity provider owns authentication; this table only
    -- owns what that identity is allowed to do here.
    cognito_sub  TEXT NOT NULL UNIQUE,
    email        CITEXT NOT NULL,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenant_members (
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','admin','member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

-- --------------------------------------------------------- BYOK provider keys --

CREATE TABLE provider_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,           -- apollo | clay | hunter | prospeo | ...
    -- The ARN, never the key. Reading the secret requires the task role, so a
    -- database dump is not a credential dump.
    secret_arn  TEXT NOT NULL,
    label       TEXT,
    status      TEXT NOT NULL DEFAULT 'untested'
                CHECK (status IN ('untested','valid','invalid','revoked')),
    last_checked_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider)
);

-- ----------------------------------------------------------------- bridges --

CREATE TABLE bridges (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Only the hash. A stolen database must not yield a working bridge token.
    token_hash    TEXT NOT NULL UNIQUE,
    linkedin_username TEXT,
    bridge_version    TEXT,
    platform      TEXT,
    status        TEXT NOT NULL DEFAULT 'offline'
                  CHECK (status IN ('offline','online','busy','error')),
    last_seen_at  TIMESTAMPTZ,
    last_error    TEXT,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX bridges_tenant_status_idx ON bridges (tenant_id, status);

-- Device-code pairing. Short-lived by construction: an unclaimed code is a
-- standing invitation to attach a bridge to someone else's tenant.
CREATE TABLE bridge_pairings (
    code        TEXT PRIMARY KEY,
    -- Null until approved, and that is the security property: the bridge that
    -- requests a code does not get to name the tenant it joins. Whoever is
    -- signed in when they approve the code decides that, so a stranger who
    -- guesses a code still cannot attach themselves to someone else's account.
    -- (These rows are therefore reached through the unscoped accessor; an
    -- unclaimed row has no tenant for RLS to match.)
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    bridge_id   UUID REFERENCES bridges(id) ON DELETE SET NULL,
    -- The plaintext bridge token, held only between approval and collection.
    -- It lives here rather than in a process dict because the control plane
    -- runs as several tasks behind a load balancer: approve can land on one
    -- task and poll on another, and an in-memory token would make pairing fail
    -- forever with "already collected". Cleared atomically on collection by
    -- `UPDATE ... WHERE token_once IS NOT NULL RETURNING token_once`, which is
    -- single-use across every task without a lock.
    token_once  TEXT,
    approved_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- targets --

CREATE TABLE targets (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('icp','role')),
    name       TEXT NOT NULL,
    brief      TEXT NOT NULL,             -- the user's words, kept verbatim
    spec       JSONB NOT NULL,            -- serialized ICP or RoleSpec
    interpretation_notes JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX targets_tenant_idx ON targets (tenant_id, created_at DESC);

-- -------------------------------------------------------- runs and traces --

CREATE TABLE runs (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    workflow   TEXT NOT NULL,
    target_id  UUID REFERENCES targets(id) ON DELETE SET NULL,
    status     TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                 ('queued','running','awaiting_bridge','awaiting_approval',
                  'succeeded','failed','cancelled')),
    -- LangGraph thread. Resuming a run means resuming this thread from the
    -- Postgres checkpointer, so it must outlive the worker that started it.
    thread_id  TEXT,
    limits     JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress   JSONB NOT NULL DEFAULT '{}'::jsonb,
    blocked_reason TEXT,
    error      TEXT,
    total_cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
    total_bridge_calls INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    ended_at   TIMESTAMPTZ
);
CREATE INDEX runs_tenant_created_idx ON runs (tenant_id, created_at DESC);
CREATE INDEX runs_status_idx ON runs (status) WHERE status IN ('queued','running','awaiting_bridge');

-- The trace UI reads this table, not LangSmith. Traces are a product surface
-- customers look at, so they cannot live behind a third-party dashboard.
CREATE TABLE run_steps (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    node       TEXT NOT NULL,
    sequence   INT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','skipped','cached')),
    input_summary  JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cost_usd   NUMERIC(12,6) NOT NULL DEFAULT 0,
    -- The number that matters most in the UI: LinkedIn calls are the scarce
    -- resource, so "this step spent none" is the headline, not a footnote.
    bridge_calls INT NOT NULL DEFAULT 0,
    provider_credits JSONB NOT NULL DEFAULT '{}'::jsonb,
    retries    INT NOT NULL DEFAULT 0,
    error      TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,
    UNIQUE (run_id, sequence)
);
CREATE INDEX run_steps_run_idx ON run_steps (run_id, sequence);

-- ------------------------------------------------- the cache that pays for itself --

-- Given one LinkedIn action at a time per user, never scraping the same person
-- twice is the single largest throughput lever in the product.
CREATE TABLE people (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    dedupe_key  TEXT NOT NULL,
    linkedin_urn      TEXT,
    linkedin_username TEXT,
    full_name   TEXT NOT NULL,
    headline    TEXT,
    location    TEXT,
    -- Full Person model including per-field evidence. JSONB rather than columns
    -- because the evidence trail is the point and it is not a fixed shape.
    doc         JSONB NOT NULL,
    email_status TEXT NOT NULL DEFAULT 'unknown',
    phone_status TEXT NOT NULL DEFAULT 'unknown',
    sourced_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    enriched_at TIMESTAMPTZ,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, dedupe_key)
);
CREATE INDEX people_tenant_refreshed_idx ON people (tenant_id, refreshed_at DESC);
CREATE INDEX people_doc_gin ON people USING gin (doc jsonb_path_ops);

CREATE TABLE companies (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    name       TEXT NOT NULL,
    domain     TEXT,
    linkedin_urn TEXT,
    doc        JSONB NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, dedupe_key)
);

-- A person's score is only meaningful against the target it was scored for,
-- so it lives on the join and not on the person.
CREATE TABLE run_people (
    run_id     UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    person_id  UUID NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    target_id  UUID REFERENCES targets(id) ON DELETE SET NULL,
    score      NUMERIC(5,2),
    score_rationale TEXT,
    -- True when this row cost no LinkedIn call because the cache answered.
    from_cache BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, person_id)
);
CREATE INDEX run_people_score_idx ON run_people (run_id, score DESC NULLS LAST);

-- --------------------------------------------- write safety (Phase 3, built now) --

-- Server-side mirror of the bridge's local action ledger. The bridge-local copy
-- alone is not enough: a user who re-pairs on a new machine comes back with an
-- empty ledger and would send the same connection request a second time.
CREATE TABLE action_ledger (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    -- The natural key that survives a re-pair, a new run, and a new target.
    person_urn TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('connect_with_person','send_message')),
    run_id     UUID REFERENCES runs(id) ON DELETE SET NULL,
    status     TEXT NOT NULL DEFAULT 'dispatched'
               CHECK (status IN ('dispatched','acked','performed','failed','deduped')),
    performed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);
-- The constraint that actually prevents a double-send across every code path.
CREATE UNIQUE INDEX action_ledger_once_idx
    ON action_ledger (tenant_id, person_urn, action)
    WHERE status IN ('dispatched','acked','performed');

CREATE TABLE usage_daily (
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day        DATE NOT NULL,
    bridge_calls INT NOT NULL DEFAULT 0,
    write_actions INT NOT NULL DEFAULT 0,
    cost_usd   NUMERIC(12,6) NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

-- ------------------------------------------------------- row-level security --
-- Belt to the repository layer's braces. The app connects as a non-superuser
-- role and sets app.tenant_id per transaction; a query that forgets its WHERE
-- clause returns nothing rather than someone else's pipeline.

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'provider_keys','bridges','bridge_pairings','targets','runs','run_steps',
    'people','companies','run_people','action_ledger','usage_daily'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING '
      '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
  END LOOP;
END $$;

-- tenant_members is the one tenant-scoped table that cannot key on app.tenant_id:
-- it is what the control plane reads to LEARN which tenants a user may act as,
-- which happens before any tenant has been chosen. Scoping it by app.tenant_id
-- would make membership lookup return nothing and lock every user out of their
-- own account. So it is scoped by the user instead — same guarantee, different
-- key — rather than left as the single unprotected hole in the model.
ALTER TABLE tenant_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_members FORCE ROW LEVEL SECURITY;
CREATE POLICY member_self_isolation ON tenant_members USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);
