#!/bin/sh
# read-only login for ClickHouse dictionaries and a writer for the inventory loader
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE ch_reader LOGIN PASSWORD '${PG_CH_READER_PASSWORD}';
GRANT USAGE ON SCHEMA inventory TO ch_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA inventory TO ch_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA inventory GRANT SELECT ON TABLES TO ch_reader;

CREATE ROLE inventory_loader LOGIN PASSWORD '${PG_LOADER_PASSWORD}';
GRANT USAGE ON SCHEMA inventory TO inventory_loader;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA inventory TO inventory_loader;
SQL
