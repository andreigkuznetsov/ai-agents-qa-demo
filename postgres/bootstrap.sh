#!/bin/sh
(
set -eu

if [ "$POSTGRES_PASSWORD" = "$QA_MIGRATE_PASSWORD" ] ||
   [ "$POSTGRES_PASSWORD" = "$QA_API_PASSWORD" ] ||
   [ "$QA_MIGRATE_PASSWORD" = "$QA_API_PASSWORD" ]; then
    echo "bootstrap failed: PostgreSQL roles require distinct credentials" >&2
    exit 1
fi

# psql quotes passwords read from the environment.
psql -X --quiet --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv migrate_password QA_MIGRATE_PASSWORD
\getenv api_password QA_API_PASSWORD
\getenv database POSTGRES_DB

BEGIN;
CREATE ROLE qa_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'migrate_password';
CREATE ROLE qa_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'api_password';
REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE :"database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO qa_migrate, qa_api;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA qa AUTHORIZATION qa_migrate;
REVOKE CREATE ON SCHEMA qa FROM PUBLIC;
GRANT USAGE ON SCHEMA qa TO qa_api;
COMMIT;
SQL
)
