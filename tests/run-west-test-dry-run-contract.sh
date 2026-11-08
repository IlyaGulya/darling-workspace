#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mise exec -- uv run --frozen -- python -B \
  tests/west_test_contracts/dry_run_contract.py
