-- Policy defaults for tenants created *after* 0002 ran.
--
-- 0002 seeded approval_policies and field_policies for the tenants that existed
-- when it was applied. That is a backfill, not a rule — so every workspace
-- created afterwards started with no policy rows at all, and
-- ``memory.approval_mode`` falls back to require_approval when a row is missing.
-- The result would be a new customer whose enrichment and research sat in a
-- review queue on their first run, with nothing in the product explaining why.
--
-- A trigger rather than application code, because tenants are created on more
-- than one path (signup, a test fixture, a support script) and a default that
-- depends on the caller remembering is not a default.

-- SECURITY DEFINER, owned by gtmos_system, for a reason worth stating: both
-- policy tables are under FORCE row-level security, which applies to the table
-- owner too. At the moment a tenant row is inserted, `app.tenant_id` is either
-- unset or still points at whoever was acting a moment ago — so an invoker-rights
-- trigger cannot write the new tenant's own defaults and tenant creation fails
-- outright. gtmos_system is the role that already exists for exactly this class
-- of work (authentication, pairing, bootstrap), and search_path is pinned
-- because a definer-rights function that resolves names dynamically is a
-- privilege escalation waiting to be found.
CREATE OR REPLACE FUNCTION seed_tenant_policies() RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    -- Conservative on purpose (ADR-010): anything that reaches a human being
    -- waits for a human being. Reads and enrichment run themselves.
    INSERT INTO approval_policies (tenant_id, action_class, mode)
    VALUES (NEW.id, 'outbound_message',   'require_approval'),
           (NEW.id, 'connection_request', 'require_approval'),
           (NEW.id, 'enrichment',         'auto'),
           (NEW.id, 'research',           'auto'),
           (NEW.id, 'refresh_read',       'auto')
    ON CONFLICT DO NOTHING;

    -- TTLs track observed decay: titles move fastest (65.8%/yr), emails
    -- 20-30%/yr, phones 15-20%/yr. See docs/product-research.md.
    INSERT INTO field_policies (tenant_id, field, ttl_days, min_confidence)
    VALUES (NEW.id, 'title',      90,  0.70),
           (NEW.id, 'company',    90,  0.70),
           (NEW.id, 'email',      180, 0.80),
           (NEW.id, 'phone',      270, 0.80),
           (NEW.id, 'headcount',  120, 0.60),
           (NEW.id, 'tech_stack', 120, 0.60)
    ON CONFLICT DO NOTHING;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

ALTER FUNCTION seed_tenant_policies() OWNER TO gtmos_system;

DROP TRIGGER IF EXISTS tenants_seed_policies ON tenants;
CREATE TRIGGER tenants_seed_policies
    AFTER INSERT ON tenants
    FOR EACH ROW EXECUTE FUNCTION seed_tenant_policies();

-- Any tenant created between 0002 and this migration.
INSERT INTO approval_policies (tenant_id, action_class, mode)
SELECT t.id, c.action_class, c.mode
FROM tenants t,
     (VALUES ('outbound_message','require_approval'),
             ('connection_request','require_approval'),
             ('enrichment','auto'),
             ('research','auto'),
             ('refresh_read','auto')) AS c(action_class, mode)
ON CONFLICT DO NOTHING;

INSERT INTO field_policies (tenant_id, field, ttl_days, min_confidence)
SELECT t.id, f.field, f.ttl_days, f.min_conf
FROM tenants t,
     (VALUES ('title', 90, 0.70),
             ('company', 90, 0.70),
             ('email', 180, 0.80),
             ('phone', 270, 0.80),
             ('headcount', 120, 0.60),
             ('tech_stack', 120, 0.60)) AS f(field, ttl_days, min_conf)
ON CONFLICT DO NOTHING;
