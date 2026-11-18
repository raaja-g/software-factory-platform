#!/bin/bash
# One-shot: create the `litellm` role and database in the factory Postgres if they are missing.
# Env: LITELLM_DB_PASSWORD (required), PGHOST, PGUSER, PGPASSWORD_FILE (default /run/secrets/pg_password).
set -euo pipefail
: "${LITELLM_DB_PASSWORD:?LITELLM_DB_PASSWORD must be set}"
export PGHOST="${PGHOST:-postgres}" PGUSER="${PGUSER:-factory}" PGDATABASE="${PGDATABASE:-postgres}"
PGPASSWORD="$(cat "${PGPASSWORD_FILE:-/run/secrets/pg_password}")"
export PGPASSWORD
psql -v ON_ERROR_STOP=1 -v pw="$LITELLM_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE litellm LOGIN PASSWORD %L', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'litellm') \gexec
SELECT format('ALTER ROLE litellm PASSWORD %L', :'pw') \gexec
SELECT 'CREATE DATABASE litellm OWNER litellm'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'litellm') \gexec
SQL
