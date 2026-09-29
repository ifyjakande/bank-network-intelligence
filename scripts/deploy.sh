#!/usr/bin/env bash
# Runs on the demo instance (sent over SSM by the deploy workflow): move to a commit and
# bring the stack up. `make up` is idempotent: changed images rebuild, new migrations
# apply, data volumes stay.
set -euo pipefail
ref="${1:?usage: deploy.sh <commit-sha>}"
cd /opt/bni
git fetch --quiet origin "$ref"
git checkout --quiet --force "$ref"
make up
echo "deployed $(git rev-parse --short HEAD)"
