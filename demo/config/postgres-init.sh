#!/bin/sh
# Two databases with separate owners: the product's incident state and the demo's payments data.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
CREATE USER oncall WITH PASSWORD '${ONCALL_DB_PASSWORD}';
CREATE DATABASE oncall OWNER oncall;
CREATE USER payments WITH PASSWORD '${PAYMENTS_DB_PASSWORD}';
CREATE DATABASE payments OWNER payments;
SQL
