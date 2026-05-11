#!/bin/sh
# Apply DB schema before the process (default: python -m bot). Idempotent at head.
set -eu
cd /app
alembic upgrade head
exec "$@"
