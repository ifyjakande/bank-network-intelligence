#!/usr/bin/env bash
# Creates .env.stack with random credentials for the local stack (never committed).
# ClickHouse gets sha256 hashes; clients get the plaintext.
set -euo pipefail
cd "$(dirname "$0")/.."
out=.env.stack
if [[ -f $out ]]; then
    echo "$out already exists, leaving it alone"
    exit 0
fi
rand() { openssl rand -hex 16; }
# macOS has shasum, Amazon Linux has sha256sum
if command -v sha256sum >/dev/null; then
    sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
else
    sha() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }
fi

ch_admin=$(rand); ch_etl=$(rand); ch_grafana=$(rand)
pg_super=$(rand); pg_reader=$(rand); pg_loader=$(rand)
umask 077
cat > "$out" <<ENV
COMPOSE_PROJECT_NAME=bni
CH_CLUSTER_SECRET=$(rand)
CH_ADMIN_PASSWORD=$ch_admin
CH_ADMIN_PASSWORD_SHA256=$(sha "$ch_admin")
CH_ETL_PASSWORD=$ch_etl
CH_ETL_PASSWORD_SHA256=$(sha "$ch_etl")
CH_GRAFANA_PASSWORD=$ch_grafana
CH_GRAFANA_PASSWORD_SHA256=$(sha "$ch_grafana")
POSTGRES_PASSWORD=$pg_super
PG_CH_READER_PASSWORD=$pg_reader
PG_LOADER_PASSWORD=$pg_loader
GRAFANA_ADMIN_PASSWORD=$(rand)
BENCH_PASSWORD=$(rand)
ENV
echo "wrote $out"
