#!/usr/bin/env bash
# Launch the Mini Financial Terminal (API + web UI) on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

# Prefer the project virtualenv so dependencies never land in the system Python.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

# This process and the container share one SQLite database (MFT_DATA_DIR in .env).
# Sequential use is fine; two servers writing at once across a Docker bind mount is
# not — and a second server on another port silently splits your data in two.
if docker ps --filter name=5milliondollars --filter status=running -q 2>/dev/null | grep -q .; then
  cat >&2 <<'MSG'
The 5milliondollars container is already running on the same database.
Use http://localhost:8000, or stop it first:

  docker compose stop
MSG
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${PORT} is already in use. Stop what is on it, or set PORT=... " >&2
  exit 1
fi

if ! "$PY" -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies into ${PY}..."
  "$PY" -m pip install -q -r requirements.txt
fi

DB="$("$PY" -c 'from backend.config import settings; print(settings.database_url)' 2>/dev/null || echo "unknown")"

echo "Mini Financial Terminal -> http://localhost:${PORT}"
echo "  web UI      http://localhost:${PORT}          (DATA tab browses every command)"
echo "  API docs    http://localhost:${PORT}/docs"
echo "  CLI         ${PY} -m cli.terminal"
echo "  database    ${DB}"
exec "$PY" -m uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}" "$@"
