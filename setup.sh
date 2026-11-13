#!/usr/bin/env bash
set -euo pipefail

# Pocket Nutrition recognition server — First-time setup
# Usage: ./setup.sh

echo "=== Pocket Nutrition recognition server setup ==="

# Check prerequisites
command -v python3.12 >/dev/null 2>&1 || { echo "Error: python3.12 is required."; exit 1; }

# Virtual environment
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3.12 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Environment
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in RECOGNITION_API_KEY and RECOGNITION_WORKER_KEY"
fi

# Dependencies
echo "Installing dependencies..."
pip install --upgrade pip >/dev/null
pip install -r requirements-dev.txt

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env: fill in RECOGNITION_API_KEY and RECOGNITION_WORKER_KEY. The server refuses to"
echo "     start in worker mode without a worker key of 16+ characters (openssl rand -hex 32 makes one)"
echo "  2. Start the server: (set -a; . ./.env; set +a; exec .venv/bin/uvicorn app.main:build --factory --port 8000)"
echo "  3. Check it: curl localhost:8000/health"
echo "  4. Development without a worker (fixed sample result, never for a deployment):"
echo "       RECOGNITION_PIPELINE=simulator RECOGNITION_API_KEY=dev .venv/bin/uvicorn app.main:build --factory --reload --port 8000"
echo "  5. Run tests: .venv/bin/pytest (in a shell where no RECOGNITION_* variable is exported)"
echo "  6. Using Claude Code? CLAUDE.md has all the context."
