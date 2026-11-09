#!/bin/bash
# Creates schemas, extensions and the three DB roles (spec section 1).
# Runs once, on first start of an empty data volume.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$DB_APP_PASSWORD" \
  -v rw_pw="$DB_MASTER_RW_PASSWORD" \
  -v ro_pw="$DB_MASTER_RO_PASSWORD" \
  -v dbname="$POSTGRES_DB" <<'SQL'
CREATE SCHEMA IF NOT EXISTS singularity;
CREATE SCHEMA IF NOT EXISTS master;

-- Extensions live in the internal schema (the app role finds them via its search_path; `public` stays locked).
CREATE EXTENSION IF NOT EXISTS vector SCHEMA singularity;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA singularity;

CREATE ROLE singularity_app       LOGIN PASSWORD :'app_pw';
CREATE ROLE singularity_master_rw LOGIN PASSWORD :'rw_pw';
CREATE ROLE singularity_master_ro LOGIN PASSWORD :'ro_pw';

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA singularity FROM PUBLIC;
REVOKE ALL ON SCHEMA master FROM PUBLIC;

GRANT CONNECT ON DATABASE :"dbname" TO singularity_app, singularity_master_rw, singularity_master_ro;

-- singularity_app: owns internal state only. No access to master data, nothing in public.
GRANT USAGE, CREATE ON SCHEMA singularity TO singularity_app;
ALTER DEFAULT PRIVILEGES FOR ROLE singularity_app IN SCHEMA singularity
  GRANT ALL ON TABLES TO singularity_app;

-- singularity_master_rw: DDL + DML on master only (used by the gate's apply step).
GRANT USAGE, CREATE ON SCHEMA master TO singularity_master_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE singularity_master_rw IN SCHEMA master
  GRANT SELECT ON TABLES TO singularity_master_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE singularity_master_rw IN SCHEMA master
  GRANT USAGE, SELECT ON SEQUENCES TO singularity_master_ro;

-- singularity_master_ro: SELECT on master only.
GRANT USAGE ON SCHEMA master TO singularity_master_ro;

-- Known limit: as owner of its schemas, singularity_master_rw can still run DROP SCHEMA ... CASCADE
-- directly. The gate must never emit DROP SCHEMA; the executor only calls drop_managed_schema().
-- Managed-schema functions (also shipped as db/migrations/2026-11-09-managed-schemas.sql).
CREATE OR REPLACE FUNCTION singularity.create_managed_schema(p_name text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  IF p_name IS NULL OR p_name !~ '^[a-z][a-z0-9_]{0,62}$'
     OR p_name IN ('singularity', 'public', 'information_schema') OR left(p_name, 3) = 'pg_' THEN
    RAISE EXCEPTION 'invalid schema name: %', coalesce(p_name, '<null>') USING ERRCODE = 'invalid_schema_name';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = p_name) THEN
    RAISE EXCEPTION 'schema % already exists', p_name USING ERRCODE = 'invalid_schema_name';
  END IF;
  EXECUTE format('CREATE SCHEMA %I AUTHORIZATION singularity_master_rw', p_name);
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO singularity_master_ro', p_name);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE singularity_master_rw IN SCHEMA %I GRANT SELECT ON TABLES TO singularity_master_ro', p_name);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE singularity_master_rw IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO singularity_master_ro', p_name);
END
$$;

CREATE OR REPLACE FUNCTION singularity.rename_managed_schema(p_old text, p_new text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  IF p_old IS NULL OR p_old IN ('master', 'singularity', 'public', 'information_schema') OR left(p_old, 3) = 'pg_' THEN
    RAISE EXCEPTION 'schema % cannot be renamed', coalesce(p_old, '<null>') USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = p_old) THEN
    RAISE EXCEPTION 'schema % does not exist', p_old USING ERRCODE = 'invalid_schema_name';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles r ON r.oid = n.nspowner
                 WHERE n.nspname = p_old AND r.rolname = 'singularity_master_rw') THEN
    RAISE EXCEPTION 'schema % is not a managed schema', p_old USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF p_new IS NULL OR p_new !~ '^[a-z][a-z0-9_]{0,62}$'
     OR p_new IN ('singularity', 'public', 'information_schema') OR left(p_new, 3) = 'pg_' THEN
    RAISE EXCEPTION 'invalid schema name: %', coalesce(p_new, '<null>') USING ERRCODE = 'invalid_schema_name';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = p_new) THEN
    RAISE EXCEPTION 'schema % already exists', p_new USING ERRCODE = 'invalid_schema_name';
  END IF;
  EXECUTE format('ALTER SCHEMA %I RENAME TO %I', p_old, p_new);
END
$$;

CREATE OR REPLACE FUNCTION singularity.drop_managed_schema(p_name text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  v_oid oid;
BEGIN
  IF p_name IS NULL OR p_name IN ('master', 'singularity', 'public', 'information_schema') OR left(p_name, 3) = 'pg_' THEN
    RAISE EXCEPTION 'schema % cannot be dropped', coalesce(p_name, '<null>') USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT n.oid INTO v_oid FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles r ON r.oid = n.nspowner
   WHERE n.nspname = p_name AND r.rolname = 'singularity_master_rw';
  IF v_oid IS NULL THEN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = p_name) THEN
      RAISE EXCEPTION 'schema % is not a managed schema', p_name USING ERRCODE = 'insufficient_privilege';
    END IF;
    RAISE EXCEPTION 'schema % does not exist', p_name USING ERRCODE = 'invalid_schema_name';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace = v_oid)
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_type WHERE typnamespace = v_oid AND typcategory <> 'A' AND typrelid = 0)
     OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc WHERE pronamespace = v_oid) THEN
    RAISE EXCEPTION 'schema % still contains objects', p_name USING ERRCODE = 'dependent_objects_still_exist';
  END IF;
  EXECUTE format('DROP SCHEMA %I', p_name);
END
$$;

REVOKE ALL ON FUNCTION singularity.create_managed_schema(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION singularity.rename_managed_schema(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION singularity.drop_managed_schema(text) FROM PUBLIC;
-- pin the owner explicitly: the functions are SECURITY DEFINER, so their owner is who they run as.
ALTER FUNCTION singularity.create_managed_schema(text) OWNER TO CURRENT_USER;
ALTER FUNCTION singularity.rename_managed_schema(text, text) OWNER TO CURRENT_USER;
ALTER FUNCTION singularity.drop_managed_schema(text) OWNER TO CURRENT_USER;
-- rw needs USAGE on singularity only to be able to call the functions below (no table privileges).
GRANT USAGE ON SCHEMA singularity TO singularity_master_rw;
GRANT EXECUTE ON FUNCTION singularity.create_managed_schema(text) TO singularity_master_rw;
GRANT EXECUTE ON FUNCTION singularity.rename_managed_schema(text, text) TO singularity_master_rw;
GRANT EXECUTE ON FUNCTION singularity.drop_managed_schema(text) TO singularity_master_rw;
SQL
