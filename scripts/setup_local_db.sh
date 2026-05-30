#!/usr/bin/env bash
# Provision the local Postgres role and database for workspace-mcp-gateway.
# Credentials match .env / .env.example (workspace_mcp / wmcp_local_dev_pw).
#
# Run with:  sudo ./scripts/setup_local_db.sh
# (Re-runnable: the role and database are only created if missing.)
set -euo pipefail

DB_NAME="workspace_mcp"
DB_USER="workspace_mcp"
DB_PASSWORD="wmcp_local_dev_pw"

# Create the login role if it doesn't already exist.
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
  END IF;
END \$\$;
SQL

# Create the database (CREATE DATABASE cannot run inside a DO block / transaction).
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
  echo "created database ${DB_NAME}"
else
  echo "database ${DB_NAME} already exists"
fi

echo "done: role '${DB_USER}' and database '${DB_NAME}' are ready"
