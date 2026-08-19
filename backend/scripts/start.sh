#!/bin/sh
set -eu

# Default behavior is to run migrations before starting the API.
# Set RUN_MIGRATIONS_ON_START=0 to skip this on startup.
if [ "${RUN_MIGRATIONS_ON_START:-1}" = "1" ]; then
  echo "Running database migrations..."
  .venv/bin/alembic upgrade head
fi

echo "Starting GapRadar API..."
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000