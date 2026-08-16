#!/usr/bin/env bash
# SessionStart hook: ensure the Python toolchain is ready in web sessions.
# Installs dependencies once so `pytest` and `uvicorn` work out of the box.
set -e
cd "$(dirname "$0")/.." 2>/dev/null || cd "$CLAUDE_PROJECT_DIR" 2>/dev/null || true

if ! python3 -c "import fastapi, pandas, statsmodels" 2>/dev/null; then
  echo "[session-start] Installing Python dependencies..." >&2
  pip install -q -r requirements.txt >&2 || true
fi
echo "[session-start] Mini Financial Terminal ready. Run tests with: python3 -m pytest -q" >&2
