-- Roles and grants, which until now existed only in one developer's container.
--
-- 0001 and 0002 create tables but grant nothing, and no GRANT statement existed
-- anywhere in this repository. The local database worked because a previous
-- session created `gtmos_app` and `gtmos_system` by hand — so a fresh
-- `docker run` plus the migrations produced a schema that `./run-local.sh`
-- could not read, and the sixteen tables added by 0002 were unreadable even on
-- the machine where the roles did exist.
--
-- Two fixes, and the second is the one that matters:
--
-- 1. Grant the current tables explicitly.
-- 2. ALTER DEFAULT PRIVILEGES, so the next migration's tables are covered
--    without anyone remembering to come back here. A permission model that
--    depends on memory fails silently and only in production, because
--    developers run as the owner and never notice.
--
-- The separation of the two roles is the security boundary this schema is built
-- on, so it is restated here rather than left implicit:
--
--   gtmos_app     the customer data path. NO BYPASSRLS, ever. Its queries are
--                 confined to one tenant by the policies in 0001/0002, which is
--                 what makes a forgotten WHERE clause return nothing instead of
--                 someone else's pipeline.
--   gtmos_system  authentication and pairing only: resolving a bridge token,
--                 redeeming a pairing code, reading tenants/users. It bypasses
--                 RLS, which is exactly why nothing that touches customer data
--                 may run on it (see Database.system in db.py).

-- ------------------------------------------------------------------ roles --
-- Created without a password. Dev passwords belong in run-local.sh; a deployed
-- environment authenticates through IAM and would already have these roles.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gtmos_app') THEN
        CREATE ROLE gtmos_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gtmos_system') THEN
        CREATE ROLE gtmos_system NOLOGIN BYPASSRLS;
    END IF;
END $$;

-- Belt and braces: if the role was created by hand at some point with the wrong
-- attribute, correct it here rather than trusting how it was made.
ALTER ROLE gtmos_app NOBYPASSRLS;
ALTER ROLE gtmos_system BYPASSRLS;

GRANT USAGE ON SCHEMA public TO gtmos_app, gtmos_system;

-- ----------------------------------------------------------------- grants --

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
    TO gtmos_app, gtmos_system;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
    TO gtmos_app, gtmos_system;

-- The line that stops this file from needing a visit after every migration.
-- Applies to tables created later *by the role running this migration*, which
-- is the owner in every environment we have.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gtmos_app, gtmos_system;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO gtmos_app, gtmos_system;
