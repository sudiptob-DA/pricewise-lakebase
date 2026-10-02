-- ============================================================================
-- Grant the deployed PriceWise app's service principal access to Lakebase.
-- Run this ONCE in the Lakebase SQL editor, connected as the DB owner
-- (your own Databricks identity, which owns the schemas/tables).
--
-- App service principal:
--   name:      app-164qku pricewise
--   client id: 7b043729-2428-4d30-97e3-f34145aeaa26
--
-- In Lakebase, a Databricks OAuth principal maps to a Postgres role named by
-- its client id. The role is auto-created the first time that identity
-- connects, but it starts with NO table privileges — hence these grants.
-- If the role does not yet exist (app has never connected), run the
-- CREATE ROLE line first, then the grants.
-- ============================================================================

-- Role name = the SP's client id (quoted because it contains hyphens).
\set sp '"7b043729-2428-4d30-97e3-f34145aeaa26"'

-- 1. Create the role if the app has not connected yet (safe to skip if it has).
--    Comment this out if the role already exists.
-- CREATE ROLE "7b043729-2428-4d30-97e3-f34145aeaa26" LOGIN;

-- 2. Let it connect to the database.
GRANT CONNECT ON DATABASE databricks_postgres
  TO "7b043729-2428-4d30-97e3-f34145aeaa26";

-- 3. Read access to the synced-in / public schema (search + pricing reads).
GRANT USAGE ON SCHEMA public
  TO "7b043729-2428-4d30-97e3-f34145aeaa26";
GRANT SELECT ON ALL TABLES IN SCHEMA public
  TO "7b043729-2428-4d30-97e3-f34145aeaa26";
-- Future tables synced into public inherit the grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO "7b043729-2428-4d30-97e3-f34145aeaa26";

-- 4. Read + write access to the app_data schema (Accept/Override write-back).
GRANT USAGE, CREATE ON SCHEMA app_data
  TO "7b043729-2428-4d30-97e3-f34145aeaa26";
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA app_data
  TO "7b043729-2428-4d30-97e3-f34145aeaa26";
ALTER DEFAULT PRIVILEGES IN SCHEMA app_data
  GRANT SELECT, INSERT, UPDATE ON TABLES TO "7b043729-2428-4d30-97e3-f34145aeaa26";
-- Sequences, in case pricing_decisions uses a serial/identity id.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app_data
  TO "7b043729-2428-4d30-97e3-f34145aeaa26";
ALTER DEFAULT PRIVILEGES IN SCHEMA app_data
  GRANT USAGE, SELECT ON SEQUENCES TO "7b043729-2428-4d30-97e3-f34145aeaa26";

-- ============================================================================
-- Verify (optional): list what the role can touch.
--   SELECT table_schema, table_name, privilege_type
--   FROM information_schema.role_table_grants
--   WHERE grantee = '7b043729-2428-4d30-97e3-f34145aeaa26'
--   ORDER BY table_schema, table_name;
-- ============================================================================
