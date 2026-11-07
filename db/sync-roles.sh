#!/bin/bash
# Keep the three DB roles' passwords in sync with .env on EVERY db start.
#
# Why: docker-entrypoint-initdb.d only runs on an EMPTY data volume, so a later
# .env password change silently left the roles stale and the api 500'd on every
# schema read (only the app role was ever noticed, rw/ro failed the same way).
# This runs idempotently on each container start, before the healthcheck
# reports ready — including on the very first start, where the roles exist
# already thanks to 01-schemas-roles.sh.
#
# ponytail: no per-role granularity; all three are always reset to .env values.
# Add per-role opt-out only if a role ever needs an out-of-band password.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$DB_APP_PASSWORD" \
  -v rw_pw="$DB_MASTER_RW_PASSWORD" \
  -v ro_pw="$DB_MASTER_RO_PASSWORD" <<'SQL'
ALTER ROLE singularity_app       WITH LOGIN PASSWORD :'app_pw';
ALTER ROLE singularity_master_rw WITH LOGIN PASSWORD :'rw_pw';
ALTER ROLE singularity_master_ro WITH LOGIN PASSWORD :'ro_pw';
SQL
