#!/bin/sh
# Runs as user `bot`: Alembic migrations then hand off to CMD (default: python -m bot).
set -eu
cd /app
alembic upgrade head
exec "$@"
