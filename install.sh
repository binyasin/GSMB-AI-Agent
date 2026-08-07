#!/usr/bin/env bash
# GSM Brothers AI Recovery Calling Agent — local/VPS install script.
# Usage: ./install.sh
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "== Checking Python version =="
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3.12+ first." >&2
  exit 1
fi
"$PYTHON_BIN" - <<'EOF'
import sys
if sys.version_info < (3, 12):
    print(f"ERROR: Python 3.12+ required, found {sys.version}", file=sys.stderr)
    sys.exit(1)
print(f"Python {sys.version.split()[0]} OK")
EOF

echo
echo "== Creating virtual environment (.venv) =="
if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

echo
echo "== Installing dependencies =="
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt

echo
echo "== Setting up directories =="
mkdir -p data logs reports

echo
echo "== Setting up .env =="
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit it before going live."
else
  echo ".env already exists, leaving it untouched."
fi

echo
echo "== Running the test suite =="
./.venv/bin/python -m pytest tests/ -q

cat <<'EOF'

============================================================
Install complete.

Next steps:
  1. Edit .env with your real credentials when ready (see README.md).
     Until then, TEST_MODE=true and DRY_RUN=true let you run everything
     safely with zero external accounts.
  2. Start the app:      ./.venv/bin/python -m app.main
  3. Start the dashboard: ./.venv/bin/python -m streamlit run app/dashboard.py
  4. Try the dry-run walkthrough: ./.venv/bin/python scripts/dry_run_demo.py
============================================================
EOF
