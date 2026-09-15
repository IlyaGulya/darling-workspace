#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mise exec -- uv run --no-project --with west==1.5.0 python -B \
  tests/west_test_contracts/metadata_run_report_contract.py
