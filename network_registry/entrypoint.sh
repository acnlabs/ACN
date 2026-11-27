#!/bin/sh
# The data volume is root-owned. Open it for the app user, then drop privileges.
set -eu
db="${REGISTRY_DB:-/data/registry.db}"
dir=$(dirname "$db")
mkdir -p "$dir"
chown appuser:appuser "$dir"
port="${PORT:-8000}"
exec su -s /bin/sh appuser -c "exec /app/.venv/bin/uvicorn network_registry.app:app --host 0.0.0.0 --port ${port}"
