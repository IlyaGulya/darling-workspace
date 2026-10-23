#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 -B tests/west_test_contracts/setup_local_contract.py
