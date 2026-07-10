#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 tests/west_test_contracts/execution_contract.py
