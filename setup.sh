#!/usr/bin/env bash
# First-time setup for standalone funding-arb project.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PAPER_REQUIREMENTS="$ROOT/requirements.paper.lock"

if [[ ! -f "$PAPER_REQUIREMENTS" ]]; then
  echo "Paper dependency lock is missing: $PAPER_REQUIREMENTS" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit for paper/test configuration; live trading is not supported by this fork."
fi

mkdir -p data output runs/.state
touch data/.gitkeep runs/.state/.gitkeep

if ! python3 -c "import json" 2>/dev/null; then
  echo "Python 3 required."
  exit 1
fi

printf '%s\n' "Installing/verifying hash-locked paper dependencies..."
python3 -m pip install \
  --disable-pip-version-check \
  --no-input \
  --require-hashes \
  -r "$PAPER_REQUIREMENTS"

echo ""
echo "Setup complete."
echo "  Scan:      python3 scripts/cli/scan_pure_futures_spreads.py --verbose"
echo "  Scan C&C:  python3 scripts/cli/scan_funding_arbitrage.py --venues bitget,bybit,okx,binance"
echo "  Paper run: python3 scripts/execution/run_pure_futures_spread.py --config templates/config.pure_futures.spread.json --once --verbose"
echo "  Dashboard: bash start.sh"
echo "  Tests:     python3 -m pytest scripts/tests/ -q"
