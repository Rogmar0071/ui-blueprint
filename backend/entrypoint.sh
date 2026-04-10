#!/usr/bin/env bash
set -euo pipefail
cd backend
echo "Running Alembic migrations..."
if ! python -m alembic --version > /dev/null 2>&1; then
    echo "ERROR: alembic is not installed. Add alembic>=1.13 to backend/requirements.txt." >&2
    exit 1
fi
python -m alembic -c alembic.ini upgrade head
echo "Starting server..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
