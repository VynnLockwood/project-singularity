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
SQL
