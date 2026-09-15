#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 -B tests/west_test_contracts/stock_replay_iterations_contract.py
